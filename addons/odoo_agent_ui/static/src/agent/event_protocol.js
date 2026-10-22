/**
 * The browser side of `odoo_agent_contracts.v1.ui_events` (schema version "1").
 *
 * Pure on purpose: no imports, no DOM, no Odoo. It validates what the server sends, folds run
 * events into view state, and turns transport errors into the same three failure kinds the server
 * uses -- so it is tested directly with `node --test` (static/tests/node) while the Owl component
 * stays a thin renderer.
 *
 * Fail closed: a payload that is not exactly version 1, or a run that is not one ordered sequence
 * ending in one terminal event, is not rendered as an answer. It becomes a failure the user can see.
 */

export const SCHEMA_VERSION = "1";
export const FAILURE_KINDS = Object.freeze(["permission", "policy_blocked", "temporary"]);
export const MAX_ATTEMPTS = 3;

const EVENT_TYPES = new Set([
    "run.accepted",
    "step.started",
    "step.completed",
    "message.completed",
    "record.cited",
    "run.completed",
    "run.failed",
]);
const TERMINAL = new Set(["run.completed", "run.failed"]);

const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const isText = (value) => typeof value === "string" && value.length > 0;

/** A failure produced in the browser, shaped like the server's `RunFailureV1`. */
export function clientFailure(code) {
    const catalog = {
        "not-authorized": {
            kind: "permission",
            title: "Access denied",
            detail: "Your account is not allowed to use the agent screen.",
            retryable: false,
        },
        "connection-lost": {
            kind: "temporary",
            title: "Connection interrupted",
            detail: "Odoo could not be reached. Nothing was changed; retrying is safe.",
            retryable: true,
        },
        "server-error": {
            kind: "temporary",
            title: "Something went wrong",
            detail: "The server could not complete the request. Nothing was changed.",
            retryable: true,
        },
        "request-refused": {
            kind: "temporary",
            title: "Request not accepted",
            detail: "The server refused this request as malformed.",
            retryable: false,
        },
        "response-not-understood": {
            kind: "temporary",
            title: "Unexpected response",
            detail: "The server answered in a format this screen does not understand, so nothing is shown as an answer.",
            retryable: false,
        },
    };
    const entry = catalog[code] || catalog["server-error"];
    return { ...entry, code: catalog[code] ? code : "server-error", no_action_performed: true, client: true };
}

export function validateBootstrap(payload) {
    if (!isObject(payload) || payload.schema_version !== SCHEMA_VERSION) {
        return { ok: false, code: "response-not-understood" };
    }
    if (payload.contract !== "odoo_agent_ui.bootstrap" || payload.writes_enabled !== false) {
        return { ok: false, code: "response-not-understood" };
    }
    if (typeof payload.demo_mode !== "boolean" || !Array.isArray(payload.workflows)) {
        return { ok: false, code: "response-not-understood" };
    }
    const seen = new Set();
    for (const workflow of payload.workflows) {
        if (
            !isObject(workflow) ||
            !isText(workflow.id) ||
            !isText(workflow.label) ||
            !isText(workflow.group) ||
            !["available", "unavailable"].includes(workflow.availability) ||
            typeof workflow.demo !== "boolean" ||
            seen.has(workflow.id) ||
            (workflow.demo && !payload.demo_mode) ||
            (workflow.availability === "unavailable") !== isText(workflow.reason)
        ) {
            return { ok: false, code: "response-not-understood" };
        }
        seen.add(workflow.id);
    }
    return { ok: true, value: payload };
}

function validEvent(event, runId, demo, index) {
    if (
        !isObject(event) ||
        event.schema_version !== SCHEMA_VERSION ||
        !EVENT_TYPES.has(event.type) ||
        event.run_id !== runId ||
        event.sequence !== index ||
        event.demo !== demo
    ) {
        return false;
    }
    switch (event.type) {
        case "run.accepted":
            return isText(event.workflow_id);
        case "step.started":
        case "step.completed":
            return isText(event.step_id) && isText(event.label);
        case "message.completed":
            return isText(event.text);
        case "record.cited":
            return (
                isText(event.model) &&
                Number.isInteger(event.record_id) &&
                event.record_id > 0 &&
                isText(event.display_name) &&
                Array.isArray(event.fields) &&
                event.fields.every((field) => isObject(field) && isText(field.label) && isText(field.value))
            );
        case "run.failed": {
            const failure = event.failure;
            return (
                isObject(failure) &&
                FAILURE_KINDS.includes(failure.kind) &&
                isText(failure.code) &&
                isText(failure.title) &&
                isText(failure.detail) &&
                failure.retryable === (failure.kind === "temporary") &&
                failure.no_action_performed === true
            );
        }
        default:
            return true;
    }
}

export function validateRunResponse(payload) {
    if (
        !isObject(payload) ||
        payload.schema_version !== SCHEMA_VERSION ||
        payload.contract !== "odoo_agent_ui.run" ||
        !isText(payload.run_id) ||
        typeof payload.demo !== "boolean" ||
        !Array.isArray(payload.events) ||
        payload.events.length === 0
    ) {
        return { ok: false, code: "response-not-understood" };
    }
    const events = payload.events;
    for (let index = 0; index < events.length; index++) {
        if (!validEvent(events[index], payload.run_id, payload.demo, index)) {
            return { ok: false, code: "response-not-understood" };
        }
        if (TERMINAL.has(events[index].type) !== (index === events.length - 1)) {
            return { ok: false, code: "response-not-understood" };
        }
    }
    return { ok: true, value: payload };
}

export function newRun() {
    return { runId: null, demo: false, status: "running", steps: [], message: null, records: [], failure: null };
}

/** Fold one validated event into a run's view state. Returns a new object; never mutates. */
export function applyEvent(run, event) {
    const next = { ...run, steps: run.steps.map((step) => ({ ...step })), records: [...run.records] };
    switch (event.type) {
        case "run.accepted":
            next.runId = event.run_id;
            next.demo = event.demo;
            break;
        case "step.started":
            next.steps.push({ id: event.step_id, label: event.label, state: "running" });
            break;
        case "step.completed":
            for (const step of next.steps) {
                if (step.id === event.step_id) {
                    step.state = "done";
                }
            }
            break;
        case "message.completed":
            next.message = event.text;
            break;
        case "record.cited":
            next.records.push({
                key: `${event.model}:${event.record_id}`,
                model: event.model,
                recordId: event.record_id,
                displayName: event.display_name,
                fields: event.fields.map((field, index) => ({ key: index, ...field })),
                demo: event.demo,
            });
            break;
        case "run.completed":
            next.status = "completed";
            break;
        case "run.failed":
            next.status = "failed";
            next.failure = event.failure;
            for (const step of next.steps) {
                if (step.state === "running") {
                    step.state = "interrupted";
                }
            }
            break;
    }
    next.runId = next.runId || event.run_id;
    next.demo = event.demo;
    return next;
}

/**
 * Map a transport error to a client failure: permission, malformed request, lost connection.
 * `connectionLost` is decided by the caller with `instanceof ConnectionLostError`, which this
 * import-free module cannot do itself.
 */
export function failureFromError(error, { connectionLost = false } = {}) {
    if (connectionLost) {
        return clientFailure("connection-lost");
    }
    const name = (error && (error.exceptionName || (error.data && error.data.name))) || "";
    if (name === "odoo.exceptions.AccessError") {
        return clientFailure("not-authorized");
    }
    if (name === "odoo.exceptions.UserError" || name === "odoo.exceptions.ValidationError") {
        return clientFailure("request-refused");
    }
    return clientFailure("server-error");
}

/** Workflows grouped for an <optgroup> list, in the order the server sent them. */
export function groupWorkflows(workflows) {
    const groups = [];
    const byName = new Map();
    for (const workflow of workflows) {
        if (!byName.has(workflow.group)) {
            const group = { name: workflow.group, workflows: [] };
            byName.set(workflow.group, group);
            groups.push(group);
        }
        byName.get(workflow.group).workflows.push(workflow);
    }
    return groups;
}

export const REASON_LABELS = Object.freeze({
    "control-plane-not-connected": "not connected yet",
    "workflow-not-authorized": "not available to you",
});

export function firstAvailable(workflows) {
    return workflows.find((workflow) => workflow.availability === "available") || null;
}
