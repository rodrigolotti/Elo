// Gera docs/verificacao.png: a página pública /verificar de um compromisso
// da demo, do topo até o fim da tabela de checagens.
//
//   node scripts/captura_verificacao.mjs [saida.png]
//
// Sobe a API com um banco temporário, roda a demo (demo_api.py) com os CNPJs
// de exemplo fixos dos testes, em vez de sortear números que podem ser de
// empresas reais, escolhe o compromisso que passou por aprovação humana e o
// fotografa num Chrome (ou Edge) headless. Falha se a página não tiver todas
// as checagens OK ou se o trecho fotografado mostrar outro CNPJ, e-mail ou
// caminho de arquivo.
//
// Variáveis: CHROME (caminho do navegador), PYTHON (padrão: python).

import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SAIDA = resolve(process.argv[2] ?? join(RAIZ, "docs", "verificacao.png"));
const LARGURA = 780;
const PYTHON = process.env.PYTHON ?? "python";
const CHROME = process.env.CHROME ?? [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find((p) => { try { return statSync(p).isFile(); } catch { return false; } });

const PORTA_API = 8600 + Math.floor(Math.random() * 300);
const PORTA_CDP = 9600 + Math.floor(Math.random() * 300);
const BASE = `http://127.0.0.1:${PORTA_API}`;
const temporario = mkdtempSync(join(tmpdir(), "elo-captura-"));
const processos = [];
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

// A demo da API, mas com os CNPJs de exemplo dos testes (11.222.333/0001-81 e
// o do embarcador), que são números de documentação, não de empresas.
const DEMO_COM_CNPJ_FIXO = `
import sys
sys.path[:0] = [".", "tests"]
import httpx2 as httpx
import demo_api
from conftest import CNPJ_EMBARC, CNPJ_TRANSP
fila = iter([CNPJ_TRANSP, CNPJ_EMBARC])
demo_api.cnpj_aleatorio = lambda: next(fila)
with httpx.Client(base_url=sys.argv[1], timeout=10) as cliente:
    for cid in demo_api.executar(cliente):
        print(cid)
`;

async function aguardarUrl(url) {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(url)).ok) return; } catch { /* ainda subindo */ }
    await esperar(200);
  }
  throw new Error(`não respondeu: ${url}`);
}

async function principal() {
  if (!CHROME) throw new Error("Chrome/Edge não encontrado: defina CHROME");
  processos.push(spawn(PYTHON, ["-m", "uvicorn", "elo.api:app", "--port", String(PORTA_API)],
    { cwd: RAIZ, env: { ...process.env, ELO_DB: join(temporario, "elo.db") }, stdio: "ignore" }));
  await aguardarUrl(`${BASE}/openapi.json`);

  const ids = execFileSync(PYTHON, ["-c", DEMO_COM_CNPJ_FIXO, BASE],
    { cwd: RAIZ, env: { ...process.env, PYTHONIOENCODING: "utf-8" } })
    .toString("utf-8").match(/cp_[0-9a-f]+/g) ?? [];
  let escolhido = null;
  for (const id of ids) {
    const rel = await (await fetch(`${BASE}/compromissos/${id}/verificacao`)).json();
    if (rel.valido && rel.verificacoes.some((v) => v.detalhe.includes("aprovada por"))) escolhido = id;
  }
  if (!escolhido) throw new Error(`nenhum compromisso válido com aprovação humana entre ${ids}`);

  processos.push(spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORTA_CDP}`,
    `--user-data-dir=${join(temporario, "perfil")}`, "--no-first-run",
    "--no-default-browser-check", "--hide-scrollbars", "about:blank"], { stdio: "ignore" }));
  await aguardarUrl(`http://127.0.0.1:${PORTA_CDP}/json/version`);
  const alvo = (await (await fetch(`http://127.0.0.1:${PORTA_CDP}/json/list`)).json())
    .find((t) => t.type === "page");
  const ws = new WebSocket(alvo.webSocketDebuggerUrl);
  await new Promise((r, e) => { ws.onopen = r; ws.onerror = e; });
  let id = 0;
  const pendentes = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (pendentes.has(msg.id)) { pendentes.get(msg.id)(msg); pendentes.delete(msg.id); }
  };
  const cdp = (method, params = {}) => new Promise((r) => {
    pendentes.set(++id, r);
    ws.send(JSON.stringify({ id, method, params }));
  });

  await cdp("Emulation.setDeviceMetricsOverride",
    { width: LARGURA, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.enable");
  await cdp("Page.navigate", { url: `${BASE}/verificar/${escolhido}` });
  await esperar(1500);
  const { result } = await cdp("Runtime.evaluate", { returnByValue: true, expression: `(() => {
    const tabela = document.querySelector("table");
    const fim = Math.ceil(tabela.getBoundingClientRect().bottom + window.scrollY + 8);
    const titulo = document.querySelector("main").innerText.split("Checagens")[0];
    return { altura: fim, texto: titulo + tabela.innerText,
             checagens: tabela.querySelectorAll("td.st").length,
             ok: [...tabela.querySelectorAll("td.st")].filter((e) => e.textContent === "OK").length };
  })()` });
  const pagina = result.result.value;

  // o que aparece no recorte: tudo OK, e nada que identifique gente ou máquina
  if (pagina.checagens === 0 || pagina.ok !== pagina.checagens) {
    throw new Error(`checagens: ${pagina.ok} de ${pagina.checagens} OK`);
  }
  const cnpjs = pagina.texto.match(/\d{2}\.\d{3}\.\d{3}\/\d{4}-\d{2}/g) ?? [];
  const estranhos = cnpjs.filter((c) => c !== "11.222.333/0001-81");
  if (estranhos.length) throw new Error(`CNPJ que não é o de exemplo na imagem: ${estranhos}`);
  if (/@|[A-Za-z]:[\\/]|\/home\/|\/Users\//.test(pagina.texto)) {
    throw new Error("e-mail ou caminho de arquivo no trecho da imagem");
  }

  const foto = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
    clip: { x: 0, y: 0, width: LARGURA, height: pagina.altura, scale: 1 } });
  ws.close();
  writeFileSync(SAIDA, Buffer.from(foto.result.data, "base64"));
  console.log(`${SAIDA}: ${LARGURA}x${pagina.altura}, ${pagina.ok} de ${pagina.checagens} `
    + `checagens OK (compromisso ${escolhido}, dados fictícios)`);
}

let codigo = 0;
try {
  await principal();
} catch (erro) {
  console.error(`FALHOU: ${erro.message}`);
  codigo = 1;
} finally {
  for (const p of processos) p.kill();
  await esperar(500);
  try { rmSync(temporario, { recursive: true, force: true }); } catch { /* navegador soltando */ }
}
process.exit(codigo);
