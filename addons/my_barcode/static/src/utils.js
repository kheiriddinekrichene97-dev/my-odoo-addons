import { formatFloat } from "@web/core/utils/numbers";

let audioContext = null;

/** Short beep confirming a successful scan. Sound is best-effort only. */
export function playSuccess() {
    try {
        audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = 1200;
        gain.gain.value = 0.08;
        oscillator.connect(gain).connect(audioContext.destination);
        oscillator.start();
        oscillator.stop(audioContext.currentTime + 0.08);
    } catch {
        // Ignore: the browser may block audio until the user interacts with the page.
    }
}

export function playError() {
    try {
        new Audio("/barcodes/static/src/audio/error.mp3").play().catch(() => {});
    } catch {
        // Ignore: see playSuccess.
    }
}

export function formatQty(value) {
    return Number.isInteger(value) ? String(value) : formatFloat(value, { digits: [16, 2] });
}
