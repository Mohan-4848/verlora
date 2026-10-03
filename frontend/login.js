// Log in / register-your-shop page.
import { api, saveToken } from './api.js';

const $ = (sel) => document.querySelector(sel);

// already logged in → straight to the portal
api.get('/api/auth/me').then(() => { location.href = './'; }).catch(() => {});

function showTab(tab) {
  document.querySelectorAll('.auth-tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  $('#loginForm').hidden = tab !== 'login';
  $('#registerForm').hidden = tab !== 'register';
  hideAlert();
  history.replaceState(null, '', tab === 'register' ? '?register' : location.pathname);
  (tab === 'register' ? $('#regShopName') : $('#loginEmail')).focus();
}
document.querySelectorAll('.auth-tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
document.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.goto)));
if (location.search.includes('register')) showTab('register');

document.querySelectorAll('.pw-toggle').forEach((b) => b.addEventListener('click', () => {
  const input = document.getElementById(b.dataset.toggle);
  input.type = input.type === 'password' ? 'text' : 'password';
}));

function showAlert(msg) { const a = $('#authAlert'); a.textContent = msg; a.hidden = false; }
function hideAlert() { $('#authAlert').hidden = true; }
function clearErrors(form) { form.querySelectorAll('[data-err]').forEach((e) => { e.textContent = ''; }); }
function fieldError(form, name, msg) {
  const el = form.querySelector(`[data-err="${name}"]`);
  if (el) el.textContent = msg;
}

async function submitting(form, fn) {
  const btn = form.querySelector('.auth-submit');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="auth-spinner"></span>';
  hideAlert();
  try { await fn(); } finally { btn.disabled = false; btn.innerHTML = label; }
}

// ---------------- login ----------------
$('#loginForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const form = e.target;
  submitting(form, async () => {
    try {
      const me = await api.post('/api/auth/login', { email: $('#loginEmail').value, password: $('#loginPassword').value });
      saveToken(me.token);
      location.href = './';
    } catch (err) { showAlert(err.message); }
  });
});

// ---------------- register ----------------

$('#regPassword').addEventListener('input', (e) => {
  const v = e.target.value;
  const score = [v.length >= 8, /[A-Z]/.test(v) || /[^\w]/.test(v), /\d/.test(v), v.length >= 12].filter(Boolean).length;
  const meter = $('#pwMeter');
  meter.style.width = `${score * 25}%`;
  meter.dataset.level = score;
});

$('#registerForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const form = e.target;
  clearErrors(form);
  const data = Object.fromEntries(new FormData(form));
  let bad = false;
  if (!data.shop_name.trim()) { fieldError(form, 'shop_name', "Enter your shop's name"); bad = true; }
  if (!data.owner_name.trim()) { fieldError(form, 'owner_name', 'Enter your name'); bad = true; }
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(data.email.trim())) { fieldError(form, 'email', 'Enter a valid email'); bad = true; }
  if (data.password.length < 8) { fieldError(form, 'password', 'Use at least 8 characters'); bad = true; }
  if (data.password !== $('#regPassword2').value) { fieldError(form, 'password2', "Passwords don't match"); bad = true; }
  if (data.pincode && !/^\d{6}$/.test(data.pincode.trim())) { fieldError(form, 'pincode', 'PIN code must be 6 digits'); bad = true; }
  if (bad) return;

  submitting(form, async () => {
    try {
      const me = await api.post('/api/auth/register', data);
      saveToken(me.token);
      location.href = './?welcome=1';
    } catch (err) {
      if (err.errors) Object.entries(err.errors).forEach(([k, v]) => fieldError(form, k, v));
      showAlert(err.message);
    }
  });
});
