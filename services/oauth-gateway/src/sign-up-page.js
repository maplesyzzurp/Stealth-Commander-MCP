const document = String.raw`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#080b0b">
  <title>Create an account | Stealth Commander</title>
  <link rel="icon" type="image/png" href="/icon.png">
  <style>
    :root { --bg: #080b0b; --panel: #101615; --border: #293632; --text: #e9f1ed; --muted: #98aaa1; --accent: #93e5bb; color-scheme: dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-size: 16px; line-height: 1.6; }
    a { color: var(--accent); text-underline-offset: .22em; }
    a:hover { color: #b2f1d0; }
    a:focus-visible, button:focus-visible, input:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
    .shell { width: min(1104px, calc(100% - 40px)); margin-inline: auto; }
    header { border-bottom: 1px solid var(--border); }
    .header-inner { min-height: 88px; display: flex; align-items: center; justify-content: space-between; gap: 24px; }
    .brand { display: inline-flex; align-items: center; gap: 12px; color: var(--text); text-decoration: none; font-size: 15px; font-weight: 650; }
    .brand img { width: 40px; height: 40px; padding: 5px; border-radius: 8px; background: #edf2ef; }
    nav { display: flex; gap: 24px; font-size: 13px; }
    nav a { color: var(--muted); text-decoration: none; }
    main { padding: 70px 20px; }
    .card { max-width: 460px; margin: auto; padding: 36px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); }
    .eyebrow { margin: 0 0 14px; color: var(--accent); font: 500 11px/1.5 ui-monospace, "SFMono-Regular", Consolas, monospace; letter-spacing: .1em; text-transform: uppercase; }
    h1 { margin: 0 0 14px; font-size: 36px; line-height: 1.15; letter-spacing: -.04em; }
    .intro { margin: 0 0 28px; color: var(--muted); font-size: 15px; }
    label { display: block; margin: 18px 0 7px; font-size: 14px; font-weight: 600; }
    input { width: 100%; min-height: 46px; padding: 11px 12px; border: 1px solid #52685d; border-radius: 6px; background: var(--bg); color: var(--text); font: inherit; }
    .hint { margin: 7px 0 0; color: var(--muted); font-size: 13px; }
    button { width: 100%; min-height: 46px; margin-top: 26px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent); color: #09150e; font-family: inherit; font-size: 15px; font-weight: 650; line-height: 1.5; cursor: pointer; }
    button:hover { background: #b2f1d0; }
    button:disabled { opacity: .65; cursor: wait; }
    #result { min-height: 25px; margin: 18px 0 0; color: #e6c6a4; font-size: 14px; overflow-wrap: anywhere; }
    #sign-in-link { font-size: 14px; }
    .help { margin: 20px 0 0; padding-top: 22px; border-top: 1px solid var(--border); color: var(--muted); font-size: 13px; }
    footer { padding: 0 20px 32px; color: var(--muted); text-align: center; font-size: 12px; }
    @media (max-width: 520px) { .header-inner { min-height: 80px; flex-wrap: wrap; padding-block: 16px; gap: 14px; } main { padding: 36px 18px; } .card { padding: 27px 23px; } h1 { font-size: 32px; } }
  </style>
</head>
<body>
  <header>
    <div class="shell header-inner">
      <a class="brand" href="/" aria-label="Stealth Commander home"><img src="/icon.png" width="40" height="40" alt=""><span>Stealth Commander</span></a>
      <nav aria-label="Main navigation"><a href="/about">About</a><a href="/support">Support</a></nav>
    </div>
  </header>
  <main>
    <section class="card" aria-labelledby="sign-up-title">
      <p class="eyebrow">Get started</p>
      <h1 id="sign-up-title">Create an account.</h1>
      <p class="intro">Create your Stealth Commander account, then pair a Windows PC from your dashboard.</p>
      <form id="signup" method="post" action="/sign-up">
        <label for="email">Email</label><input id="email" name="email" type="email" maxlength="254" required autocomplete="username">
        <label for="password">Password</label><input id="password" name="password" type="password" minlength="12" maxlength="1024" required autocomplete="new-password" aria-describedby="password-hint">
        <p class="hint" id="password-hint">Use 12 to 1,024 characters.</p>
        <label for="confirm">Confirm password</label><input id="confirm" name="confirm" type="password" minlength="12" maxlength="1024" required autocomplete="new-password">
        <button id="submit" type="submit">Create account</button>
        <p id="result" role="status" aria-live="polite"></p>
        <a id="sign-in-link" href="/sign-in" hidden>Sign in to your account</a>
      </form>
      <noscript><p>Enable JavaScript in your browser to create an account.</p></noscript>
      <p class="help">Already have an account? <a href="/sign-in">Sign in</a>.<br>Need help? <a href="/support">Visit Support</a>.</p>
    </section>
  </main>
  <footer>Stealth Commander</footer>
  <script>
    const form = document.getElementById('signup');
    const result = document.getElementById('result');
    const submit = document.getElementById('submit');
    const signInLink = document.getElementById('sign-in-link');
    let inFlight = false;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (inFlight) return;
      signInLink.hidden = true;
      const values = new FormData(form);
      const rawEmail = values.get('email');
      const email = typeof rawEmail === 'string' ? rawEmail.trim() : '';
      const password = values.get('password');
      if (!email || email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        result.textContent = 'Enter a valid email address of no more than 254 characters.';
        return;
      }
      if (typeof password !== 'string' || password.length < 12 || password.length > 1024) {
        result.textContent = 'Use a password of 12 to 1,024 characters.';
        return;
      }
      if (password !== values.get('confirm')) {
        result.textContent = 'Passwords do not match. Enter the same password in both fields.';
        return;
      }
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      inFlight = true;
      submit.disabled = true;
      result.textContent = 'Creating your account.';
      try {
        const response = await fetch('/api/auth/register', {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password })
        });
        if (response.ok) { location.assign('/dashboard'); return; }
        if (response.status === 409) {
          result.textContent = 'An account with this email already exists. Sign in using the link below.';
          signInLink.hidden = false;
        } else if (response.status === 400) result.textContent = 'Check your email and use a password of 12 to 1,024 characters, then try again.';
        else if (response.status === 429) result.textContent = 'Too many account creation attempts. Please wait before trying again.';
        else if (response.status >= 500) result.textContent = 'Account creation is temporarily unavailable. Try again shortly. If it continues, visit Support.';
        else result.textContent = 'Your account could not be created. Check your details and try again.';
      } catch {
        result.textContent = controller.signal.aborted
          ? 'The account creation request timed out. Check your connection and try again.'
          : 'Could not reach account creation. Check your connection and try again.';
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        submit.disabled = false;
      }
    });
  </script>
</body>
</html>`;

export function signUpPage(method = 'GET') {
  return new Response(method === 'HEAD' ? null : document, { headers: {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer'
  } });
}
