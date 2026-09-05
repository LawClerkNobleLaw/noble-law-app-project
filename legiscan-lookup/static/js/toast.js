/* toast.js — the one transient confirmation in this app, with an
 * optional undo.
 *
 * Built for position changes (see setPosition in bill_clients.js). A
 * client's support/oppose/watch dropdown writes on change: no
 * confirmation, no undo, and an accidental Support-to-Oppose flip was
 * the most consequential single click in the product and also the
 * cheapest. The answer isn't a confirmation dialog on every change —
 * that would tax the ordinary case to guard the rare one — it's saying
 * what just happened and leaving a way back for as long as anyone would
 * plausibly notice.
 *
 * Deliberately not a general notification system. One toast at a time
 * (a second replaces the first), no stacking, no queue, no levels.
 * Errors still go where they went before: the page's own #error region,
 * which stays on screen until the problem is dealt with. A toast is for
 * something that succeeded.
 */

const TOAST_TIMEOUT_MS = 9000;

let toastEl = null;
let toastTimer = null;

function dismissToast() {
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  if (toastEl) { toastEl.remove(); toastEl = null; }
}

/* showToast('Anthropic PBC set to Oppose on CA SB1159', {
 *   actionLabel: 'Undo', onAction: () => ...,
 * })
 *
 * role="status" rather than an alert: this is confirmation of something
 * the user just did on purpose, and it should be announced without
 * interrupting whatever they're doing next.
 */
function showToast(message, options) {
  const opts = options || {};
  dismissToast();

  toastEl = document.createElement('div');
  toastEl.className = 'toast';
  toastEl.setAttribute('role', 'status');
  toastEl.setAttribute('aria-live', 'polite');

  const text = document.createElement('span');
  text.className = 'toast-text';
  text.textContent = message;
  toastEl.appendChild(text);

  if (opts.actionLabel && opts.onAction) {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'toast-action';
    action.textContent = opts.actionLabel;
    action.addEventListener('click', () => {
      // Dismiss first: the action reloads the page's data, and a toast
      // left standing would be describing a change that has just been
      // taken back.
      dismissToast();
      opts.onAction();
    });
    toastEl.appendChild(action);
  }

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '×';
  close.addEventListener('click', dismissToast);
  toastEl.appendChild(close);

  document.body.appendChild(toastEl);

  // Nine seconds, not the usual three or four: the whole point is the
  // undo, and noticing you picked the wrong client's position takes
  // longer than noticing a file saved.
  //
  // Paused while the pointer is over the toast or focus is inside it,
  // and restarted from full on the way out. Undo is the only recovery
  // path for an accidental position flip, and on a fixed timer it can
  // vanish mid-reach — a user tabbing toward the button, or a screen-
  // reader user still locating it after the announcement, was racing a
  // clock they couldn't see. Restarting from full rather than resuming
  // the remainder: someone who just moved off the toast has spent that
  // time reading it, not ignoring it.
  const el = toastEl;
  const timeout = opts.timeout || TOAST_TIMEOUT_MS;
  const startTimer = () => {
    if (toastEl !== el) return;   // a later toast replaced this one
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(dismissToast, timeout);
  };
  const pauseTimer = () => {
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  };
  el.addEventListener('mouseenter', pauseTimer);
  el.addEventListener('focusin', pauseTimer);
  el.addEventListener('mouseleave', startTimer);
  // focusout also fires moving between the toast's own two buttons, and
  // relatedTarget is where focus is heading — still inside means the
  // user hasn't left, so the clock stays stopped.
  el.addEventListener('focusout', (e) => {
    if (!el.contains(e.relatedTarget)) startTimer();
  });
  startTimer();
}
