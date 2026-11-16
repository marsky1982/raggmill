/**
 * 全站文本对比度实测（整改方案附录 A 的脚本化版本）。
 *
 * 一条命令：在 frontend/ 下 `npm run contrast`（可加 `-- --url http://127.0.0.1:5173`）。
 *
 * 做什么：
 *  - 用本机 Edge/Chrome 的无头模式打开应用（Node 20+ 自带 WebSocket，走 CDP，不引入依赖）；
 *  - 注入 `*{transition:none!important;animation:none!important}`，避免过渡中间态影响取色；
 *  - 依次走五个页面，并打开测试集详情、导出下拉（Radix portal）、质检报告弹层（Radix portal）；
 *  - 每个状态都遍历 document.body 的全部文本节点，按 WCAG 相对亮度算对比度，
 *    普通文字要求 ≥ 4.5:1、大字要求 ≥ 3:1（禁用态按 WCAG 豁免跳过，SVG 图形不计）；
 *  - 浅色与深色各跑一遍，任一处低于阈值即以退出码 1 结束。
 *
 * 前置：应用已在运行（.\\start.ps1 或 cd frontend && npm run dev）。默认 URL 可通过
 * CONTRAST_URL 或 --url 覆盖；浏览器路径可用 BROWSER_PATH 指定。
 *
 * 【维护约定】新增页面（或新增列表/详情/弹层等可见状态）时，必须在下方 pages/状态列表补一个状态，
 * 浅色与深色都要覆盖。**覆盖不到的状态等于未验收** —— 页面验收前先跑 `npm run contrast`。
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const argv = process.argv.slice(2);
const argValue = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
};
const URL_TARGET = argValue("--url") || process.env.CONTRAST_URL || "http://127.0.0.1:5173";

function browserPath() {
  if (process.env.BROWSER_PATH && existsSync(process.env.BROWSER_PATH)) return process.env.BROWSER_PATH;
  const pf = process.env.PROGRAMFILES || "C:\\Program Files";
  const pf86 = process.env["PROGRAMFILES(X86)"] || "C:\\Program Files (x86)";
  const candidates = [
    join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
    join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    join(pf, "Google", "Chrome", "Application", "chrome.exe"),
    join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
  ];
  const found = candidates.find((p) => existsSync(p));
  if (!found) {
    console.error("找不到 Edge/Chrome。请用 BROWSER_PATH 指定浏览器可执行文件路径。");
    process.exit(2);
  }
  return found;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchJson(url, tries = 40) {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(url);
      if (res.ok) return await res.json();
    } catch {
      // 浏览器刚启动时端口尚未就绪
    }
    await sleep(250);
  }
  throw new Error(`无法连接 ${url}`);
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      const slot = this.pending.get(msg.id);
      if (!slot) return;
      this.pending.delete(msg.id);
      clearTimeout(slot.timer);
      if (msg.error) slot.reject(new Error(`${slot.method}: ${msg.error.message}`));
      else slot.resolve(msg.result);
    });
  }

  send(method, params = {}) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} 超时`));
      }, 20000);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result.value;
  }
}

const MEASURE_JS = `(() => {
  const parse = (c) => {
    const m = /rgba?\\(([^)]+)\\)/.exec(c || "");
    if (!m) return null;
    const p = m[1].split(",").map((x) => parseFloat(x.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratioOf = (fg, bg) => {
    const a = lum(fg), b = lum(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const hidden = (el) => {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || parseFloat(s.opacity || "1") === 0) return true;
    const r = el.getBoundingClientRect();
    return r.width <= 0 || r.height <= 0;
  };
  const bgOf = (el) => {
    let node = el;
    while (node && node !== document.documentElement) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0.05) {
        // 半透明底：继续向上取到不透明层再合成本层，避免把卡片色算成纯色
        let under = null;
        for (let p = node.parentElement; p; p = p.parentElement) {
          const pc = parse(getComputedStyle(p).backgroundColor);
          if (pc && pc.a >= 0.98) { under = [pc.r, pc.g, pc.b]; break; }
        }
        if (!under) under = document.documentElement.classList.contains("dark") ? [10, 10, 11] : [250, 250, 250];
        return [c.a * c.r + (1 - c.a) * under[0], c.a * c.g + (1 - c.a) * under[1], c.a * c.b + (1 - c.a) * under[2]];
      }
      node = node.parentElement;
    }
    return document.documentElement.classList.contains("dark") ? [10, 10, 11] : [250, 250, 250];
  };

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const low = [];
  const seen = new Set();
  let checked = 0;
  while (walker.nextNode()) {
    const textNode = walker.currentNode;
    const text = (textNode.textContent || "").replace(/\\s+/g, " ").trim();
    if (!text) continue;
    const el = textNode.parentElement;
    if (!el || hidden(el)) continue;
    if (el.closest("svg")) continue;            // 图形/节点标签不按正文算
    if (el.closest('[disabled],[aria-disabled="true"]')) continue; // 禁用态 WCAG 豁免
    const style = getComputedStyle(el);
    const fg = parse(style.color);
    if (!fg || fg.a < 0.05) continue;           // 完全透明的文字没有可读性可言
    const bg = bgOf(el);
    const [r, g, b] = [fg.a * fg.r + (1 - fg.a) * bg[0], fg.a * fg.g + (1 - fg.a) * bg[1], fg.a * fg.b + (1 - fg.a) * bg[2]];
    const size = parseFloat(style.fontSize) || 16;
    const weight = parseInt(style.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    const ratio = ratioOf([r, g, b], bg);
    checked += 1;
    if (ratio + 0.01 < need) {
      const key = [Math.round(r), Math.round(g), Math.round(b), Math.round(bg[0]), Math.round(bg[1]), Math.round(bg[2]), text].join("|");
      if (!seen.has(key)) {
        seen.add(key);
        low.push({
          text: text.slice(0, 42),
          ratio: Math.round(ratio * 100) / 100,
          need,
          color: \`rgb(\${Math.round(r)}, \${Math.round(g)}, \${Math.round(b)})\`,
          background: \`rgb(\${Math.round(bg[0])}, \${Math.round(bg[1])}, \${Math.round(bg[2])})\`,
          className: String(el.className || "").slice(0, 90),
        });
      }
    }
  }
  return { checked, low };
})()`;

async function clickByText(cdp, scope, needle) {
  return cdp.eval(`(() => {
    const root = document.querySelector(${JSON.stringify(scope)});
    if (!root) return false;
    const el = [...root.querySelectorAll("button,[role=button],a")].find(
      (e) => e.textContent.replace(/\\s+/g, " ").includes(${JSON.stringify(needle)}) && e.offsetParent !== null
    );
    if (!el) return false;
    el.click();
    return true;
  })()`);
}

async function measureThemes(cdp, state, results) {
  for (const theme of ["light", "dark"]) {
    await cdp.eval(`document.documentElement.classList.toggle("dark", ${theme === "dark"})`);
    await sleep(120);
    const res = await cdp.eval(MEASURE_JS);
    results.push({ state, theme, checked: res.checked, low: res.low.length, worst: res.low.slice(0, 6) });
    console.log(`  · ${theme.padEnd(5)} 文本 ${String(res.checked).padStart(5)} 处，低对比度 ${res.low.length} 处`);
  }
  await cdp.eval(`document.documentElement.classList.toggle("dark", false)`);
}

async function openExportMenu(cdp) {
  const hit = await cdp.eval(`(() => {
    const btn = [...document.querySelectorAll("main button")].find(
      (b) => b.textContent.trim() === "导出" && b.offsetParent !== null && !b.disabled
    );
    if (!btn) return false;
    btn.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse" }));
    btn.click();
    return true;
  })()`);
  return hit;
}

async function openQualityDialog(cdp) {
  // 先选一个知识图谱，质检按钮才可点
  const selected = await cdp.eval(`(() => {
    const sel = [...document.querySelectorAll("main select")].find(
      (s) => [...s.options].some((o) => /^kg-/.test(o.value))
    );
    if (!sel) return false;
    const opt = [...sel.options].find((o) => o.value);
    if (!opt) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
    setter.call(sel, opt.value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()`);
  if (!selected) return false;
  await sleep(250);
  if (!(await clickByText(cdp, "main", "质检报告"))) return false;
  for (let i = 0; i < 40; i++) {
    if (await cdp.eval(`!!document.querySelector('[role="dialog"]')`)) return true;
    await sleep(250);
  }
  return false;
}

async function main() {
  // 先确认前端能访问（首页是 HTML，不能按 JSON 解析），避免白等浏览器启动
  const reachable = await fetch(`${URL_TARGET.replace(/\/$/, "")}/`).then((res) => res.ok).catch(() => false);
  if (!reachable) {
    console.error(`打不开 ${URL_TARGET}。请先启动应用（.\\start.ps1 或 cd frontend && npm run dev），或用 --url 指定地址。`);
    process.exit(2);
  }

  const exe = browserPath();
  const profile = mkdtempSync(join(tmpdir(), "ragas-contrast-"));
  const child = spawn(exe, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--window-size=1440,900", `--user-data-dir=${profile}`, "--remote-debugging-port=0", URL_TARGET,
  ], { stdio: "ignore" });

  let browser = null;
  try {
    const portFile = join(profile, "DevToolsActivePort");
    let port = 0;
    for (let i = 0; i < 60; i++) {
      if (existsSync(portFile)) {
        port = parseInt(readFileSync(portFile, "utf8").split("\n")[0], 10);
        if (port) break;
      }
      await sleep(250);
    }
    if (!port) throw new Error("浏览器未在时限内暴露调试端口");

    const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
    const page = targets.find((t) => t.type === "page" && t.url.startsWith(URL_TARGET))
      || targets.find((t) => t.type === "page");
    if (!page) throw new Error("找不到页面目标");

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    browser = new Cdp(ws);

    // 等前端挂载
    for (let i = 0; i < 60; i++) {
      const ready = await browser.eval(
        `document.readyState === "complete" && document.querySelectorAll("aside nav button").length >= 5`,
      );
      if (ready) break;
      await sleep(250);
    }
    // 统一注入：关掉过渡/动画，避免取到中间色
    await browser.eval(`(() => {
      const s = document.createElement("style");
      s.id = "contrast-check-still";
      s.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}";
      document.head.appendChild(s);
      return true;
    })()`);
    console.log(`目标：${URL_TARGET}\n浏览器：${exe}\n`);

    const results = [];
    const notes = [];
    // 【维护约定】新增页面必须在这里补一个状态；覆盖不到的状态等于未验收。
    // 列表/详情/弹层等新增可见状态同理，需补状态并在浅色 + 深色各跑一遍。
    const pages = [
      { state: "模型配置", nav: "模型配置" },
      { state: "文档输入", nav: "文档输入" },
      { state: "知识图谱", nav: "知识图谱" },
      { state: "测试集", nav: "测试集" },
    ];
    for (const p of pages) {
      const ok = await clickByText(browser, "aside nav", p.nav);
      if (!ok) { notes.push(`打开「${p.state}」失败`); continue; }
      await sleep(700);
      console.log(`【${p.state}】`);
      await measureThemes(browser, p.state, results);
    }

    // 测试集详情（列表行）
    const opened = await browser.eval(`(() => {
      const row = [...document.querySelectorAll("main button")].find(
        (b) => /条\\s*·/.test(b.textContent) && b.offsetParent !== null
      );
      if (!row) return false;
      row.click();
      return true;
    })()`);
    if (opened) {
      await sleep(900);
      console.log("【测试集详情】");
      await measureThemes(browser, "测试集详情", results);
    } else {
      notes.push("没有历史测试集，跳过详情/导出下拉/质检弹层");
    }

    if (opened && await openExportMenu(browser)) {
      await sleep(250);
      if (await browser.eval(`!!document.querySelector('[role="menu"]')`)) {
        console.log("【导出下拉（portal）】");
        await measureThemes(browser, "导出下拉（portal）", results);
        await browser.eval(`document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))`);
        await sleep(200);
      } else {
        notes.push("导出下拉未渲染（Radix portal 未找到）");
      }
    } else if (opened) {
      notes.push("找不到可点的「导出」按钮");
    }

    if (opened && await openQualityDialog(browser)) {
      await sleep(300);
      console.log("【质检报告弹层（portal）】");
      await measureThemes(browser, "质检报告弹层（portal）", results);
      await browser.eval(`document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))`);
      await sleep(200);
    } else if (opened) {
      notes.push("质检报告弹层未打开（缺少知识图谱或后端不可用）");
    }

    const ok = await clickByText(browser, "aside nav", "质量评估");
    if (ok) {
      await sleep(700);
      console.log("【质量评估】");
      await measureThemes(browser, "质量评估", results);
    } else {
      notes.push("打开「质量评估」失败");
    }

    // 汇总
    const light = results.filter((r) => r.theme === "light");
    const dark = results.filter((r) => r.theme === "dark");
    const lightLow = light.reduce((n, r) => n + r.low, 0);
    const darkLow = dark.reduce((n, r) => n + r.low, 0);
    console.log(`\n浅色：${light.length} 个状态、${light.reduce((n, r) => n + r.checked, 0)} 处文本，低对比度 ${lightLow} 处`);
    console.log(`深色：${dark.length} 个状态、${dark.reduce((n, r) => n + r.checked, 0)} 处文本，低对比度 ${darkLow} 处`);
    for (const note of notes) console.log(`注意：${note}`);

    const offenders = results.filter((r) => r.low > 0);
    for (const row of offenders) {
      console.log(`\n[${row.state}][${row.theme}]`);
      for (const item of row.worst) {
        console.log(`  ${item.ratio}:1（需 ${item.need}:1）文字「${item.text}」 前景 ${item.color} 背景 ${item.background} class=${item.className}`);
      }
    }

    if (lightLow > 0 || darkLow > 0) {
      console.log("\n结论：存在低对比度文本（浅色/深色均要求 0 处）。");
      process.exitCode = 1;
    } else if (notes.length) {
      console.log("\n结论：可测范围内浅/深两套低对比度均为 0；有状态未覆盖，见上方「注意」。");
      process.exitCode = 0;
    } else {
      console.log("\n结论：浅色与深色全部状态低对比度均为 0。");
      process.exitCode = 0;
    }
  } finally {
    try { browser?.ws.close(); } catch { /* 忽略关闭异常 */ }
    child.kill();
    await sleep(300);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows 可能短暂占用 */ }
  }
}

main().catch((err) => {
  console.error(`对比度实测失败：${err.message}`);
  process.exitCode = 2;
});
