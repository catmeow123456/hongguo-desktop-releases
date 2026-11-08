// Bring the beta UI to the saved account without user clicks.
// Waits for the window, presses 扫码登录 (if shown), then 获取登录二维码.
// The bridge answers instantly from the saved account, so no QR is displayed.
const se = Application('System Events');

function proc() {
  try { return se.processes.byName('红果免费短剧'); } catch (e) { return null; }
}

function find(p, names) {
  let hit = null;
  function walk(el, d) {
    if (d > 18 || hit) return;
    let kids; try { kids = el.uiElements(); } catch (e) { return; }
    for (const k of kids) {
      let n = ''; try { n = k.name() || ''; } catch (e) {}
      if (names.indexOf(n) >= 0) { hit = k; return; }
      walk(k, d + 1);
      if (hit) return;
    }
  }
  try { walk(p.windows[0], 0); } catch (e) { return null; }
  return hit;
}

function press(el) {
  try { el.actions.byName('AXPress').perform(); return true; } catch (e) {}
  try { el.click(); return true; } catch (e) {}
  return false;
}

const deadline = Date.now() + 90000;
let pressedLogin = false, pressedQr = false, done = false;
while (Date.now() < deadline && !done) {
  const p = proc();
  if (!p) { delay(1); continue; }
  if (find(p, ['退出登录'])) { done = true; break; }
  if (!pressedLogin) {
    const login = find(p, ['扫码登录']);
    if (login) { pressedLogin = press(login); delay(1.2); continue; }
  }
  if (pressedLogin && !pressedQr) {
    const get = find(p, ['获取登录二维码']);
    if (get) { pressedQr = press(get); delay(1.5); done = true; break; }
  }
  delay(1);
}
'login=' + pressedLogin + ' qr=' + pressedQr;
