/* Shared "are you sure?" dialog for destructive actions — styled with
 * the same .modal-backdrop/.modal-panel client_quickadd.js's modal
 * uses, rather than the browser's own native confirm() (which both
 * CLIENTS_BODY's and CLIENT_DETAIL_BODY's removeClient() called before
 * this existed). confirmDelete(title, message) returns a Promise<bool>,
 * so a caller just does `if (!await confirmDelete(...)) return;` in
 * place of the old `if (!confirm(...)) return;` line — same control
 * flow, just awaited. Built once and reused (not recreated per call)
 * the same way ensureQuickAddClientModal() reuses its backdrop.
 */

function confirmDelete(title, message) {
  return new Promise((resolve) => {
    let backdrop = document.getElementById('confirm-delete-backdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'confirm-delete-backdrop';
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal-panel" role="alertdialog" aria-modal="true" aria-labelledby="cd-title" aria-describedby="cd-message">
          <div class="modal-head">
            <div>
              <div class="title" id="cd-title"></div>
              <div class="sub" id="cd-message"></div>
            </div>
          </div>
          <div class="modal-actions">
            <button type="button" class="danger" id="cd-confirm">Remove</button>
            <button type="button" class="secondary" id="cd-cancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
    }
    backdrop.querySelector('#cd-title').textContent = title;
    backdrop.querySelector('#cd-message').textContent = message;
    const confirmBtn = backdrop.querySelector('#cd-confirm');
    const cancelBtn = backdrop.querySelector('#cd-cancel');
    const finish = (result) => {
      backdrop.classList.remove('show');
      confirmBtn.removeEventListener('click', onConfirm);
      cancelBtn.removeEventListener('click', onCancel);
      backdrop.removeEventListener('click', onBackdropClick);
      resolve(result);
    };
    const onConfirm = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdropClick = (e) => { if (e.target === backdrop) finish(false); };
    confirmBtn.addEventListener('click', onConfirm);
    cancelBtn.addEventListener('click', onCancel);
    backdrop.addEventListener('click', onBackdropClick);
    backdrop.classList.add('show');
  });
}
