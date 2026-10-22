/**
 * The Autonomous Agent client action: a thin Owl renderer over `event_protocol.js`.
 *
 * It asks the server what the user may select (`/odoo_agent_ui/bootstrap`), sends one message at a
 * time (`/odoo_agent_ui/run`), validates every response against the v1 UI contract and plays the
 * run's events into the conversation. It holds no authority: the server re-authorizes the workflow
 * on every run, and a demo run can only exist when the server says demo mode is on.
 */

import { Component, onPatched, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { ConnectionLostError, rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import {
    MAX_ATTEMPTS,
    REASON_LABELS,
    applyEvent,
    clientFailure,
    failureFromError,
    firstAvailable,
    groupWorkflows,
    newRun,
    validateBootstrap,
    validateRunResponse,
} from "./event_protocol";

const STEP_DELAY_MS = 250;
const MAX_MESSAGE_LENGTH = 2000;
const DEMO_SUGGESTIONS = [
    "What needs attention today?",
    "Show stock I have no permission for",
    "Read transfers (policy blocked)",
    "Check stock while Odoo is offline",
];

export class AgentApp extends Component {
    static template = "odoo_agent_ui.AgentApp";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.alive = true;
        this.nextTurnId = 1;
        this.maxMessageLength = MAX_MESSAGE_LENGTH;
        this.demoSuggestions = DEMO_SUGGESTIONS;
        this.state = useState({
            phase: "loading",
            bootstrap: null,
            bootstrapFailure: null,
            workflowId: "",
            draft: "",
            turns: [],
            running: false,
        });
        this.log = useRef("log");
        this.renderedTurns = 0;
        this.wasRunning = false;
        onWillStart(() => this.loadBootstrap());
        // Keep the newest turn in view while a run plays, without fighting a user who scrolls back
        // through history between runs.
        onPatched(() => {
            const log = this.log.el;
            const wasRunning = this.wasRunning;
            this.wasRunning = this.state.running;
            // Including the render in which a run stops, which carries its answer or failure.
            if (
                log &&
                (this.state.running || wasRunning || this.state.turns.length !== this.renderedTurns)
            ) {
                log.scrollTop = log.scrollHeight;
            }
            this.renderedTurns = this.state.turns.length;
        });
        onWillUnmount(() => {
            this.alive = false;
        });
    }

    // -- bootstrap -----------------------------------------------------------------------------

    async loadBootstrap() {
        this.state.phase = "loading";
        this.state.bootstrapFailure = null;
        try {
            const result = validateBootstrap(await rpc("/odoo_agent_ui/bootstrap", {}));
            if (!result.ok) {
                return this.failBootstrap(clientFailure(result.code));
            }
            this.state.bootstrap = result.value;
            const selected = firstAvailable(result.value.workflows);
            this.state.workflowId = selected ? selected.id : "";
            this.state.phase = "ready";
        } catch (error) {
            this.failBootstrap(
                failureFromError(error, { connectionLost: error instanceof ConnectionLostError })
            );
        }
    }

    failBootstrap(failure) {
        this.state.bootstrapFailure = failure;
        this.state.phase = "bootstrap-failed";
    }

    get groups() {
        return this.state.bootstrap ? groupWorkflows(this.state.bootstrap.workflows) : [];
    }

    get selectedWorkflow() {
        const workflows = this.state.bootstrap ? this.state.bootstrap.workflows : [];
        return workflows.find((workflow) => workflow.id === this.state.workflowId) || null;
    }

    get canSend() {
        const workflow = this.selectedWorkflow;
        return (
            !this.state.running &&
            workflow !== null &&
            workflow.availability === "available" &&
            this.state.draft.trim().length > 0
        );
    }

    optionLabel(workflow) {
        if (workflow.availability === "available") {
            return workflow.label;
        }
        return `${workflow.label} — ${REASON_LABELS[workflow.reason] || "unavailable"}`;
    }

    // -- conversation --------------------------------------------------------------------------

    onSubmit(ev) {
        ev.preventDefault();
        if (this.canSend) {
            const message = this.state.draft.trim();
            this.state.draft = "";
            this.ask(message, this.state.workflowId);
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
            this.onSubmit(ev);
        }
    }

    useSuggestion(text) {
        if (!this.state.running && this.selectedWorkflow?.availability === "available") {
            this.ask(text, this.state.workflowId);
        }
    }

    ask(message, workflowId) {
        this.state.turns.push({ id: this.nextTurnId++, role: "user", text: message });
        const turn = {
            id: this.nextTurnId++,
            role: "agent",
            request: { message, workflowId, attempt: 1 },
            waiting: true,
            run: newRun(),
        };
        this.state.turns.push(turn);
        return this.play(this.state.turns[this.state.turns.length - 1]);
    }

    retry(turn) {
        if (this.state.running || turn.request.attempt >= MAX_ATTEMPTS) {
            return;
        }
        turn.request = { ...turn.request, attempt: turn.request.attempt + 1 };
        turn.run = newRun();
        turn.waiting = true;
        return this.play(turn);
    }

    async play(turn) {
        this.state.running = true;
        try {
            let payload;
            try {
                payload = await rpc("/odoo_agent_ui/run", {
                    workflow_id: turn.request.workflowId,
                    message: turn.request.message,
                    attempt: turn.request.attempt,
                });
            } catch (error) {
                turn.waiting = false;
                turn.run = {
                    ...turn.run,
                    status: "failed",
                    failure: failureFromError(error, {
                        connectionLost: error instanceof ConnectionLostError,
                    }),
                };
                return;
            }
            const result = validateRunResponse(payload);
            turn.waiting = false;
            if (!result.ok) {
                turn.run = { ...turn.run, status: "failed", failure: clientFailure(result.code) };
                return;
            }
            for (const event of result.value.events) {
                if (!this.alive) {
                    return;
                }
                turn.run = applyEvent(turn.run, event);
                if (event.type.startsWith("step.")) {
                    await this.pause();
                }
            }
        } finally {
            if (this.alive) {
                this.state.running = false;
            }
        }
    }

    pause() {
        const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
        return new Promise((resolve) => setTimeout(resolve, reduced ? 0 : STEP_DELAY_MS));
    }

    canRetry(turn) {
        const failure = turn.run.failure;
        return Boolean(
            failure && failure.retryable && !this.state.running && turn.request.attempt < MAX_ATTEMPTS
        );
    }

    get latestRun() {
        for (let index = this.state.turns.length - 1; index >= 0; index--) {
            if (this.state.turns[index].role === "agent") {
                return this.state.turns[index];
            }
        }
        return null;
    }

    stepStateLabel(state) {
        return { running: "in progress", done: "done", interrupted: "interrupted" }[state] || state;
    }

    openRecord(record) {
        // Demo citations name no real record and are never opened.
        if (record.demo) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: record.model,
            res_id: record.recordId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("odoo_agent_ui.agent", AgentApp);
