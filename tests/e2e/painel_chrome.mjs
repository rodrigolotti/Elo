// Verificação de ponta a ponta do painel num Chrome de verdade (headless).
//
//   node tests/e2e/painel_chrome.mjs [pasta-das-capturas]
//
// Sobe a API com um banco temporário, abre o Chrome com um perfil vazio e
// percorre as telas como uma pessoa: cadastro (com o arquivo de recuperação),
// agente, mandato, aprovação, recusa, histórico, revogação, sair e entrar de
// novo, e a recuperação da chave num "outro computador" (um segundo contexto do
// navegador, com IndexedDB próprio). Este script faz o papel do agente (emite
// ofertas) e do embarcador (aceita pelo link).
//
// Confere também: chave no IndexedDB não exportável, nenhum erro de console
// ou de CSP, e nenhum corpo enviado ao servidor com chave privada.
//
// Variáveis: CHROME (caminho do navegador), PYTHON (padrão: python).

import { spawn } from "node:child_process";
import {
  mkdtempSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assinar, chavePublicaBase64, gerarParDeChaves } from "../../elo/painel/js/assinatura.js";

const PORTA_API = 8765 + Math.floor(Math.random() * 500);
const PORTA_CDP = 9333 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORTA_API}`;
const CAPTURAS = process.argv[2] ?? join(tmpdir(), "elo-e2e-capturas");
const temporario = mkdtempSync(join(tmpdir(), "elo-e2e-"));
mkdirSync(CAPTURAS, { recursive: true });
const DOWNLOADS1 = join(temporario, "downloads-computador-1");
const DOWNLOADS2 = join(temporario, "downloads-computador-2");
mkdirSync(DOWNLOADS1);
mkdirSync(DOWNLOADS2);

const CHROME = process.env.CHROME ?? [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find((p) => { try { return statSync(p).isFile(); } catch { return false; } });

const processos = [];
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
let passo = 0;
function ok(texto) { console.log(`  ✔ ${texto}`); }
function falhar(texto) { throw new Error(texto); }

// ---------- CNPJ fictício com dígitos verificadores ----------

function cnpjFicticio(base12) {
  const dv = (nums) => {
    const pesos = nums.length === 12 ? [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
                                     : [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];
    const resto = nums.reduce((s, n, i) => s + n * pesos[i], 0) % 11;
    return resto < 2 ? 0 : 11 - resto;
  };
  const n = [...base12].map(Number);
  n.push(dv(n));
  n.push(dv(n));
  const d = n.join("");
  return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`;
}
const CNPJ_TRANSP = cnpjFicticio("112223330001");
const CNPJ_EMBARC = cnpjFicticio("223334440001");

// ---------- processos ----------

async function aguardarUrl(url, limite = 20000) {
  const fim = Date.now() + limite;
  while (Date.now() < fim) {
    try { const r = await fetch(url); if (r.ok) return r; } catch { /* ainda subindo */ }
    await esperar(200);
  }
  falhar(`não respondeu: ${url}`);
}

function subir(comando, args, opcoes) {
  const p = spawn(comando, args, { stdio: ["ignore", "ignore", "pipe"], ...opcoes });
  let erros = "";
  p.stderr.on("data", (d) => { erros += d; });
  p.on("exit", (codigo) => { if (codigo) console.error(erros); });
  processos.push(p);
  return p;
}

// ---------- CDP ----------

class Pagina {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pendentes = new Map();
    this.problemas = [];   // erros de console, exceções, violações de CSP
    this.corpos = [];      // corpos de todas as requisições enviadas
    ws.addEventListener("message", (m) => this.receber(JSON.parse(m.data)));
  }

  static async conectar(url) {
    const ws = new WebSocket(url);
    await new Promise((r, e) => { ws.onopen = r; ws.onerror = e; });
    return new Pagina(ws);
  }

  // sem targetId: a primeira aba do navegador
  static async abrir(targetId) {
    const url = targetId ? `ws://127.0.0.1:${PORTA_CDP}/devtools/page/${targetId}`
      : (await (await fetch(`http://127.0.0.1:${PORTA_CDP}/json/list`)).json())
        .find((t) => t.type === "page").webSocketDebuggerUrl;
    const pagina = await Pagina.conectar(url);
    for (const dominio of ["Page", "Runtime", "Log", "Network", "DOM"]) {
      await pagina.cdp(`${dominio}.enable`);
    }
    await pagina.cdp("Emulation.setDeviceMetricsOverride",
      { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    return pagina;
  }

  receber(msg) {
    if (msg.id && this.pendentes.has(msg.id)) {
      const { resolve, reject } = this.pendentes.get(msg.id);
      this.pendentes.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      return;
    }
    if (msg.method === "Runtime.exceptionThrown") {
      this.problemas.push(msg.params.exceptionDetails.exception?.description
                          ?? msg.params.exceptionDetails.text);
    } else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      this.problemas.push(`${msg.params.entry.source}: ${msg.params.entry.text} ${msg.params.entry.url ?? ""}`);
    } else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      this.problemas.push(`console.error: ${msg.params.args.map((a) => a.description ?? a.value).join(" ")}`);
    } else if (msg.method === "Page.javascriptDialogOpening") {
      this.dialogosNativos = (this.dialogosNativos ?? 0) + 1;
      this.cdp("Page.handleJavaScriptDialog", { accept: true });
    } else if (msg.method === "Network.requestWillBeSent" && msg.params.request.postData) {
      this.corpos.push({ url: msg.params.request.url, corpo: msg.params.request.postData });
    }
  }

  cdp(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pendentes.set(id, { resolve, reject }));
  }

  async avaliar(expressao) {
    const r = await this.cdp("Runtime.evaluate",
      { expression: expressao, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) falhar(`erro no navegador: ${r.exceptionDetails.exception?.description}`);
    return r.result.value;
  }

  async ir(url) {
    await this.cdp("Page.navigate", { url });
    await this.aguardar("document.readyState === 'complete'");
  }

  async aguardar(condicao, limite = 10000) {
    const fim = Date.now() + limite;
    while (Date.now() < fim) {
      if (await this.avaliar(`!!(${condicao})`)) return;
      await esperar(100);
    }
    const texto = await this.avaliar("document.body.innerText.slice(0, 1500)");
    falhar(`tempo esgotado esperando: ${condicao}\n--- página ---\n${texto}`);
  }

  aguardarTexto(texto, limite) {
    return this.aguardar(`document.body.innerText.includes(${JSON.stringify(texto)})`, limite);
  }

  // preenche o n-ésimo controle que bate com o seletor, disparando input/change
  preencher(seletor, valor, n = 0) {
    return this.avaliar(`(() => {
      const el = document.querySelectorAll(${JSON.stringify(seletor)})[${n}];
      if (!el) throw new Error("não achei " + ${JSON.stringify(seletor)});
      el.value = ${JSON.stringify(valor)};
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    })()`);
  }

  // clica no botão (ou link) visível cujo texto é exatamente `texto`
  clicar(texto, dentro = "document") {
    return this.avaliar(`(() => {
      const raiz = ${dentro};
      const alvo = [...raiz.querySelectorAll("button, a")]
        .find((b) => b.textContent.trim() === ${JSON.stringify(texto)} && !b.disabled);
      if (!alvo) throw new Error("não achei o botão " + ${JSON.stringify(texto)});
      alvo.click();
    })()`);
  }

  async escolherArquivo(seletor, caminho) {
    const { root } = await this.cdp("DOM.getDocument", { depth: -1 });
    const { nodeId } = await this.cdp("DOM.querySelector", { nodeId: root.nodeId, selector: seletor });
    await this.cdp("DOM.setFileInputFiles", { nodeId, files: [caminho] });
  }

  // clica num item do menu pelo endereço (o texto pode ter o contador junto)
  menu(rota) {
    return this.avaliar(`document.querySelector('nav a[href="#${rota}"]').click()`);
  }

  // nenhum "null" ou "undefined" vazando para a tela
  async semLixoNaTela() {
    const lixo = await this.avaliar(`document.body.innerText.split("\\n")
      .map((l) => l.trim()).filter((l) => ["null", "undefined", "NaN", "[object Object]"].includes(l))`);
    if (lixo.length) falhar(`texto indevido na tela: ${JSON.stringify(lixo)}`);
    const iso = await this.avaliar(String.raw`(document.querySelector("main")?.innerText ?? "")
      .match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/g)`);
    if (iso) falhar(`data ISO crua na tela: ${JSON.stringify(iso)}`);
  }

  // nenhum aviso pode ficar em cima de um botão ou link de ação
  async avisosNaoCobremBotoes() {
    const cobertos = await this.avaliar(`(() => {
      const caixa = (e) => e.getBoundingClientRect();
      const cruza = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      const avisos = [...document.querySelectorAll("#avisos .aviso")].map(caixa);
      return [...document.querySelectorAll("button, a.botao, nav a")]
        .filter((b) => !b.closest("#avisos") && b.offsetParent !== null)
        .filter((b) => avisos.some((a) => cruza(a, caixa(b))))
        .map((b) => b.textContent.trim());
    })()`);
    if (cobertos.length) falhar(`aviso em cima de: ${JSON.stringify(cobertos)}`);
  }

  async capturar(nome) {
    await this.semLixoNaTela();
    await this.avisosNaoCobremBotoes();
    await esperar(150);
    const { data } = await this.cdp("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true });
    const arquivo = join(CAPTURAS, `${String(++passo).padStart(2, "0")}-${nome}.png`);
    writeFileSync(arquivo, Buffer.from(data, "base64"));
    return arquivo;
  }
}

// ---------- o agente e o embarcador (fora do navegador) ----------

async function http(metodo, rota, corpo) {
  const r = await fetch(BASE + rota, {
    method: metodo, headers: { "Content-Type": "application/json" },
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  const dados = await r.json();
  if (!r.ok) falhar(`${metodo} ${rota}: ${r.status} ${JSON.stringify(dados)}`);
  return dados;
}

function emBrasiliaDaqui(ms) {
  const d = new Date(Date.now() + ms - 3 * 3600e3);
  return d.toISOString().slice(0, 19) + "-03:00";
}

// lotação, 500 km, 5 eixos, carga geral: piso da ANTT de R$ 4.071,76
const LOTACAO_500KM = { modalidade: "lotacao", distancia_km: 500, eixos: 5 };

// a modalidade é obrigatória (protocolo 0.2); sem dizer, vai como fracionada
async function emitirOferta(agente, pedidoId, agenteId, mandatoId, valor,
                            extra = { modalidade: "fracionada" }) {
  const { rascunho } = await http("POST", "/ofertas/rascunho", {
    pedido_id: pedidoId, agente_id: agenteId, mandato_id: mandatoId,
    condicoes: { valor_centavos: valor, coleta_ate: emBrasiliaDaqui(2 * 864e5).slice(0, 10),
                 prazo_entrega_dias_uteis: 2, inclui: ["pedágio", "seguro"], ...extra },
    valida_ate: emBrasiliaDaqui(864e5),
  });
  return http("POST", "/ofertas", await assinar(rascunho, agente.privateKey, "agente-cotacao#k1"));
}

const aceitar = (oferta) => http("POST", `/ofertas/${oferta.oferta.id}/aceite`, {
  token: oferta.token_aceite, cnpj: CNPJ_EMBARC, nome: "Maria Exemplo",
  email: "maria@exemplo.com",
});

// ---------- arquivo de recuperação e mandato pela tela ----------

async function aguardarDownload(pasta, limite = 10000) {
  const fim = Date.now() + limite;
  while (Date.now() < fim) {
    const prontos = readdirSync(pasta).filter((n) => n.endsWith(".json"));
    if (prontos.length) return join(pasta, prontos[0]);
    await esperar(100);
  }
  falhar(`o arquivo de recuperação não foi baixado em ${pasta}`);
}

// baixa o arquivo, avança e digita os dois grupos sorteados da frase
async function guardarRecuperacao(p, pasta, frase, testarGruposErrados) {
  await p.clicar("Baixar arquivo de recuperação");
  const arquivo = await aguardarDownload(pasta);
  await p.clicar("Já anotei a frase e guardei o arquivo");
  await p.aguardar("document.querySelectorAll('input[data-grupo]').length === 2");
  const indices = await p.avaliar(
    "[...document.querySelectorAll('input[data-grupo]')].map((e) => Number(e.dataset.grupo))");
  if (testarGruposErrados) {
    await p.preencher("input[data-grupo]", "ZZZZZ", 0);
    await p.preencher("input[data-grupo]", "ZZZZZ", 1);
    await p.clicar("Confirmar");
    await p.aguardarTexto("Os grupos não conferem com a frase-senha");
    await p.capturar("grupos-errados");
    if (await p.avaliar("document.querySelectorAll('input[data-grupo]').length") !== 2) {
      falhar("concluiu com os grupos errados");
    }
  }
  const grupos = frase.split("-");
  await p.preencher("input[data-grupo]", grupos[indices[0] - 1].toLowerCase(), 0);
  await p.preencher("input[data-grupo]", ` ${grupos[indices[1] - 1]} `, 1);
  await p.clicar("Confirmar");
  return arquivo;
}

async function emitirMandatoPelaTela(p, captura) {
  await p.menu("/mandatos");
  await p.aguardarTexto("Novo mandato");
  await p.avaliar(`(() => { const s = document.querySelector("form select");
    s.selectedIndex = 1; s.dispatchEvent(new Event("change")); })()`);
  await p.preencher("form input[inputmode=decimal]", "6.000,00");
  await p.avaliar(`(() => {
    const [origem, destino] = document.querySelectorAll("fieldset.ufs");
    origem.querySelector("input[value=SP]").checked = true;
    destino.querySelector("input[value=SP]").checked = true;
    destino.querySelector("input[value=PR]").checked = true;
  })()`);
  await p.avaliar(`document.querySelector("fieldset.tipos input[value=carga-geral]").checked = true`);
  if (captura) await p.capturar(captura);
  await p.clicar("Emitir mandato");
  await p.aguardarTexto("emitido: até R$ 6.000,00 por frete.");
  await p.aguardar("[...document.querySelectorAll('.etiqueta.bom')].some((e) => e.textContent === 'Ativo')");
}

// passa por todas as telas logadas numa largura, conferindo layout e fotografando
const TELAS = ["/visao-geral", "/aprovacoes", "/agentes", "/mandatos", "/historico", "/seguranca"];

async function percorrerTelas(p, largura, altura, prefixo) {
  await p.cdp("Emulation.setDeviceMetricsOverride",
    { width: largura, height: altura, deviceScaleFactor: 1, mobile: largura < 500 });
  for (const rota of TELAS) {
    await p.avaliar(`location.hash = '#${rota}'`);
    await p.aguardar(`location.hash === '#${rota}' && document.querySelector('main h1') `
      + "&& !document.querySelector('main .carregando')");
    await esperar(200);
    const m = await p.avaliar(`({
      largura: document.documentElement.scrollWidth, janela: window.innerWidth,
      // qualquer elemento (fora de tabelas e do menu, que rolam por dentro) além da borda
      vazando: [...document.querySelectorAll("main *, #topo *")]
        .filter((e) => !e.closest("table, nav") && e.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 3).map((e) => e.tagName + "." + e.className),
      linhasDoMenu: new Set([...document.querySelectorAll("nav a")].map((a) => a.offsetTop)).size,
      demo: [...document.querySelectorAll("button")].some((b) =>
        b.textContent.trim() === "Simular pedido de cotação" && b.offsetParent !== null),
      topo: document.querySelector("#topo").offsetHeight,
      cortados: (() => {
        const nav = document.querySelector("nav").getBoundingClientRect();
        return [...document.querySelectorAll("nav a")]
          .filter((a) => a.getBoundingClientRect().right > nav.right + 1).map((a) => a.textContent);
      })() })`);
    if (m.largura > m.janela || m.vazando.length) {
      falhar(`${rota} em ${largura} px passa da borda: ${m.largura} px; ${JSON.stringify(m.vazando)}`);
    }
    if (m.linhasDoMenu !== 1) falhar(`${rota} em ${largura} px: menu em ${m.linhasDoMenu} linhas`);
    if (largura < 500 && m.demo) falhar("o botão de demonstração aparece no celular");
    if (largura >= 1000 && (!m.demo || m.topo > 66 || m.cortados.length)) {
      falhar(`topo no desktop: ${JSON.stringify(m)}`);
    }
    if (rota === "/visao-geral") {
      // em cada oferta, o motivo mais grave (vermelho) vem antes dos outros
      const fora = await p.avaliar(`[...document.querySelectorAll(".vg-motivos")].filter((m) => {
        const s = [...m.children].map((c) => c.classList.contains("risco"));
        return s.indexOf(false) !== -1 && s.lastIndexOf(true) > s.indexOf(false);
      }).length`);
      if (fora) falhar(`Visão geral: ${fora} oferta(s) com motivo grave depois de um mais leve`);
    }
    await p.capturar(`${prefixo}-${rota.slice(1)}`);
  }
}

const tokenDa = (p) => p.avaliar(`JSON.parse(sessionStorage.getItem("elo.sessao")).token`);
const comToken = (token, rota) =>
  fetch(BASE + rota, { headers: { Authorization: `Bearer ${token}` } }).then((r) => r.json());

// ---------- roteiro ----------

async function roteiro() {
  console.log(`API em ${BASE}; capturas em ${CAPTURAS}`);
  subir(process.env.PYTHON ?? "python", ["-m", "uvicorn", "elo.api:app", "--port",
    String(PORTA_API)], { env: { ...process.env, ELO_DB: join(temporario, "elo.db"),
                                 ELO_ESTIMATIVA_CEP: "desligada",  // sem rede
                                 ELO_DEMO: "ligado" } });
  await aguardarUrl(`${BASE}/openapi.json`);
  if (!CHROME) falhar("Chrome/Edge não encontrado: defina CHROME");
  subir(CHROME, ["--headless=new", `--remote-debugging-port=${PORTA_CDP}`,
    `--user-data-dir=${join(temporario, "perfil")}`, "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "about:blank"]);
  await aguardarUrl(`http://127.0.0.1:${PORTA_CDP}/json/version`);
  const p = await Pagina.abrir();
  const versao = await (await fetch(`http://127.0.0.1:${PORTA_CDP}/json/version`)).json();
  const navegador = await Pagina.conectar(versao.webSocketDebuggerUrl);
  await navegador.cdp("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: DOWNLOADS1 });

  console.log("1. Entrar sem chave no navegador");
  await p.ir(`${BASE}/painel/`);
  await p.aguardarTexto("Nenhuma transportadora tem acesso guardado neste computador.");
  await p.capturar("entrar-vazio");
  ok("tela de entrada sem organização");

  console.log("2. Cadastro da organização");
  await p.clicar("Cadastrar transportadora");
  await p.aguardarTexto("Importante: o acesso da transportadora fica guardado neste computador");
  await p.preencher("input[name=cnpj]", CNPJ_TRANSP);
  await p.preencher("input[name=razao_social]", "Transportadora Fictícia Ltda");
  await p.capturar("cadastro");
  await p.clicar("Cadastrar transportadora");
  const bloqueado = await p.avaliar("location.hash");
  if (bloqueado !== "#/cadastro") falhar("enviou sem marcar o 'Entendi'");
  await p.avaliar(`document.querySelector(".confirmacao input").checked = true`);
  await p.clicar("Cadastrar transportadora");
  await p.aguardar("document.querySelector('.frase-senha')", 15000);
  if (!(await p.avaliar("!!JSON.parse(sessionStorage.getItem('elo.sessao'))?.token"))) {
    falhar("depois do cadastro a sessão deveria estar aberta");
  }
  ok("organização cadastrada, sessão aberta");
  const lerFrase = async () => {
    const f = await p.avaliar("document.querySelector('.frase-senha').textContent");
    if (!/^[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){4}$/.test(f)) falhar(`frase: ${f}`);
    return f;
  };
  const semMenu = async (quando) => {
    if (await p.avaliar("!!document.querySelector('nav') || [...document.querySelectorAll('button')]"
        + ".some((b) => b.textContent.trim() === 'Sair')")) {
      falhar(`o menu ou o 'Sair' aparecem ${quando}`);
    }
  };
  const fraseA = await lerFrase();
  await p.aguardarTexto("Sem isso, se você trocar de computador ou o navegador for limpo, "
    + "perde o acesso à conta da transportadora.");
  await p.aguardarTexto("Anote a frase em papel e guarde longe do arquivo.");
  await p.aguardarTexto("Este navegador pode apagar seu acesso se faltar espaço");

  // 6: numa tela de notebook, as ações aparecem sem rolar; 7: uma caixa de aviso só
  await p.cdp("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 650, deviceScaleFactor: 1, mobile: false });
  await esperar(200);
  const abaixoDaDobra = await p.avaliar(`[...document.querySelectorAll(".passo-recuperacao button")]
    .filter((b) => b.getBoundingClientRect().bottom > window.innerHeight)
    .map((b) => b.textContent.trim())`);
  if (abaixoDaDobra.length) falhar(`botões abaixo da dobra em 1280x650: ${abaixoDaDobra}`);
  const caixas = await p.avaliar("document.querySelectorAll('main .caixa').length");
  if (caixas !== 1) falhar(`o passo 2 tem ${caixas} caixas de aviso; deveria ter 1`);
  await p.capturar("frase-senha-notebook");
  await p.cdp("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  ok("passo 2: botões visíveis sem rolar em 1280x650 e uma caixa de aviso só, com o motivo");

  // 4 e 9a: sem menu; sair pede confirmação; continuar volta o endereço para #/cadastro
  await semMenu("durante o passo 2");
  await p.avaliar("location.hash = '#/agentes'");
  await p.aguardar("document.querySelector('dialog').open");
  await p.aguardarTexto("Sair sem guardar o arquivo de recuperação?");
  await p.capturar("sair-do-passo-2");
  await p.clicar("Continuar no passo", "document.querySelector('dialog')");
  await p.aguardar("!document.querySelector('dialog').open && location.hash === '#/cadastro'");
  if (await lerFrase() !== fraseA) falhar("ao continuar no passo, a frase-senha mudou");
  await semMenu("depois de 'Continuar no passo'");
  ok("sair pede confirmação; 'Continuar no passo' volta o endereço para #/cadastro, mesma frase");

  // 9b: F5 no passo 2 retoma o passo (com frase nova e aviso sobre o arquivo antigo)
  await p.cdp("Page.reload");
  await esperar(300);
  await p.aguardar("document.readyState === 'complete' && document.querySelector('.frase-senha')",
    15000);
  const fraseB = await lerFrase();
  if (fraseB === fraseA) falhar("depois do F5 o passo deveria gerar outra frase");
  await p.aguardarTexto("Se você baixou um arquivo antes de recarregar a página, descarte-o");
  await semMenu("depois do F5");
  await p.capturar("passo-2-depois-do-f5");
  ok("F5 no passo 2: o passo volta (frase nova, aviso para descartar arquivo antigo, sem menu)");
  // no F5 o Chrome bloqueia a pergunta "sair da página?" porque, no roteiro, ninguém
  // clicou de verdade (cliques sintéticos não contam como gesto); com uma pessoa, ela aparece
  p.problemas = p.problemas.filter((x) => !(x.includes("beforeunload") && x.includes("user gesture")));

  // quem sai mesmo assim vê, em toda tela, que está sem recuperação
  await p.avaliar("location.hash = '#/agentes'");
  await p.aguardar("document.querySelector('dialog').open");
  await p.clicar("Sair mesmo assim", "document.querySelector('dialog')");
  await p.aguardar("location.hash === '#/agentes'");
  await p.aguardarTexto("A transportadora ainda não tem arquivo de recuperação.");
  await p.aguardar("document.querySelector('nav a[href=\"#/seguranca\"] .contador')");
  await p.capturar("faixa-sem-recuperacao");
  ok("saiu sem recuperação: faixa de alerta nas telas e '!' na aba Segurança");

  // cria pela faixa, na aba Segurança (primeira vez: mensagem sem "arquivo anterior")
  await p.clicar("Criar agora");
  await p.aguardarTexto("Ainda não criado");
  await p.clicar("Criar arquivo de recuperação");
  await p.aguardar("document.querySelector('.frase-senha')", 15000);
  const frase1 = await lerFrase();
  const arquivo1 = await guardarRecuperacao(p, DOWNLOADS1, frase1, true);
  await p.aguardarTexto("Arquivo de recuperação registrado. Guarde o arquivo e a frase em lugares separados.");
  if (await p.avaliar("document.body.innerText.includes('anterior')")) {
    falhar("na primeira chave de recuperação não pode falar em arquivo anterior");
  }
  await p.aguardar("document.querySelector('nav a[href=\"#/seguranca\"]') && "
    + "!document.querySelector('nav a[href=\"#/seguranca\"] .contador')");
  await p.menu("/agentes");
  await p.aguardarTexto("Novo agente");
  if (await p.avaliar("!!document.querySelector('.faixa-recuperacao')")) {
    falhar("a faixa de 'sem recuperação' continuou depois de criar o arquivo");
  }
  ok("arquivo criado pela aba Segurança: mensagem de primeira vez, faixa e '!' somem");

  const chave = await p.avaliar(`new Promise((ok, erro) => {
    const pedido = indexedDB.open("elo-painel");
    pedido.onsuccess = () => {
      const tx = pedido.result.transaction("chaves", "readonly");
      const g = tx.objectStore("chaves").getAll();
      g.onsuccess = async () => {
        const [r] = g.result;
        let exportou = true;
        try { await crypto.subtle.exportKey("pkcs8", r.privada); } catch { exportou = false; }
        ok({ total: g.result.length, tipo: r.privada.constructor.name,
             algoritmo: r.privada.algorithm.name, extraivel: r.privada.extractable,
             usos: r.privada.usages, exportou, cnpj: r.cnpj, chavePublica: r.chavePublica });
      };
    };
    pedido.onerror = () => erro(pedido.error);
  })`);
  if (chave.total !== 1 || chave.tipo !== "CryptoKey" || chave.algoritmo !== "Ed25519"
      || chave.extraivel !== false || chave.exportou) {
    falhar(`chave no IndexedDB inesperada: ${JSON.stringify(chave)}`);
  }
  ok(`IndexedDB: CryptoKey Ed25519, extractable=false, exportKey("pkcs8") recusado`);

  console.log("3. Criação de agente");
  const agente = await gerarParDeChaves();
  const pubAgente = await chavePublicaBase64(agente.publicKey);
  await p.preencher("form input", "Agente de cotação", 0);
  await p.preencher("form input", "agente-cotacao#k1", 1);
  await p.preencher("form textarea", "isto não é uma chave");
  await p.clicar("Cadastrar agente");
  await p.aguardarTexto("A chave pública do agente não parece válida");
  await p.preencher("form textarea", pubAgente);
  await p.clicar("Cadastrar agente");
  await p.aguardarTexto("Agente “Agente de cotação” cadastrado");
  await p.aguardar("document.querySelectorAll('tbody tr').length === 1");
  await p.capturar("agentes");
  ok("chave inválida recusada no formulário; agente criado");

  console.log("4. Definição de mandato");
  await emitirMandatoPelaTela(p, "mandato-formulario");
  ok("mandato emitido e ativo");

  // o agente (fora do navegador) pega os ids pela própria API
  const sessaoToken = await p.avaliar(`JSON.parse(sessionStorage.getItem("elo.sessao")).token`);
  const comSessao = (rota) => fetch(BASE + rota, { headers: { Authorization: `Bearer ${sessaoToken}` } })
    .then((r) => r.json());
  const [{ agente_id: agenteId }] = await comSessao("/agentes");
  const [{ mandato: { id: mandatoId } }] = await comSessao("/mandatos");
  const sem = await fetch(`${BASE}/mandatos`);
  if (sem.status !== 401) falhar("GET /mandatos sem sessão deveria dar 401");

  console.log("5. Agente emite ofertas (2 fora do mandato, 1 dentro)");
  const pedido = await http("POST", "/pedidos", { cnpj_solicitante: CNPJ_EMBARC, canal: "email",
    nome_solicitante: "Comercial Exemplo de Alimentos Ltda (fictícia)",
    carga: { origem_cep: "01000-000", origem_uf: "SP", destino_cep: "80000-000",
             destino_uf: "PR", peso_kg: 3200, volume_litros: 14500, tipo: "carga-geral" } });
  const aprovar = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 900_000, LOTACAO_500KM);
  const recusar = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 750_000,
                                     { modalidade: "lotacao", eixos: 5 });  // sem distância
  const dentro = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 485_000);
  const semModalidade = await fetch(`${BASE}/ofertas/rascunho`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pedido_id: pedido.id, agente_id: agenteId, mandato_id: mandatoId,
      condicoes: { valor_centavos: 485_000, coleta_ate: emBrasiliaDaqui(2 * 864e5).slice(0, 10),
                   prazo_entrega_dias_uteis: 2 },
      valida_ate: emBrasiliaDaqui(864e5) }) });
  if (semModalidade.status !== 400
      || !(await semModalidade.json()).detail.includes("precisa informar a modalidade")) {
    falhar("oferta sem modalidade deveria ser recusada (protocolo 0.2)");
  }
  ok("oferta sem modalidade é recusada pela API (obrigatória no protocolo 0.2)");
  if (aprovar.status !== "aguardando_aprovacao" || dentro.status !== "enviada") {
    falhar("status das ofertas inesperado");
  }

  console.log("6. Fila de aprovação: aprovar e recusar");
  await p.menu("/aprovacoes");
  await p.aguardar("document.querySelectorAll('article.cartao').length === 2");
  await p.aguardar("document.querySelector('nav .contador')?.textContent === '2'");
  await p.capturar("fila-aprovacao");
  ok("duas ofertas na fila, com contador no menu");
  for (const trecho of ["Comercial Exemplo de Alimentos Ltda (fictícia)",
    "Primeiro frete com este embarcador", "Carga geral · 3.200 kg · 14,5 m³",
    "Peso cubado: 4.350 kg (300 kg/m³), maior que o peso real", "vence em 23 h",
    "SP (01000-000) → PR (80000-000)"]) {
    await p.aguardarTexto(trecho);
  }
  const hierarquia = await p.avaliar(`(() => {
    const c = document.querySelector("article.cartao");
    return { valor: c.querySelector(".c-valor").textContent, rota: c.querySelector(".c-rota").textContent };
  })()`);
  if (!/^R\$ \d/.test(hierarquia.valor) || !hierarquia.rota.includes("→")) {
    falhar(`cartão sem valor em destaque e rota abaixo: ${JSON.stringify(hierarquia)}`);
  }
  if (await p.avaliar("document.querySelector('main').innerText.includes('14500 L')")) {
    falhar("o volume ainda aparece em litros");
  }
  ok("cartão: nome do embarcador, primeiro frete, volume em m³, peso cubado e 'vence em 23 h'");
  for (const trecho of ["Frete e piso da ANTT", "Lotação · 5 eixos", "500 km (informada pelo agente)",
    "R$ 18,00/km", "R$ 4.071,76 · Carga geral, 5 eixos · tabela vigente desde 30/09/2026",
    "Acima do piso da ANTT em R$ 4.928,24.",
    "Distância não informada: não foi possível conferir o piso da ANTT.",
    "Não disponível (estimativa por CEP desligada neste servidor)"]) {
    await p.aguardarTexto(trecho);
  }
  const caixasPorCartao = await p.avaliar(
    "[...document.querySelectorAll('article.cartao')].map((c) => c.querySelectorAll('.caixa').length)");
  if (caixasPorCartao.some((n) => n !== 1)) falhar(`caixas por cartão: ${caixasPorCartao}`);
  ok("uma caixa por cartão; lotação sem distância vai para aprovação com o motivo; piso e folga");

  const cartao = (id) => `[...document.querySelectorAll("article.cartao")].find((c) => c.innerText.includes(${JSON.stringify(id)}))`;
  await p.clicar("Aprovar", cartao(aprovar.oferta.id));
  await p.aguardar("document.querySelector('dialog').open");
  await p.preencher("dialog input", "Ana Gestora");
  await p.capturar("dialogo-aprovacao");
  await p.clicar("Aprovar oferta", "document.querySelector('dialog')");
  await p.aguardarTexto("Frete de R$ 9.000,00 (SP → PR) aprovado e enviado ao embarcador.");
  await p.aguardar("document.querySelectorAll('article.cartao').length === 1");
  ok("oferta aprovada com assinatura do navegador");

  await p.clicar("Recusar", cartao(recusar.oferta.id));
  await p.aguardar("document.querySelector('dialog').open");
  await p.avaliar(`document.querySelector("dialog input").value`).then((v) => {
    if (v !== "Ana Gestora") falhar("o nome do responsável não foi lembrado");
  });
  await p.preencher("dialog textarea", "Valor acima da tabela do cliente");
  await p.capturar("dialogo-recusa");
  await p.clicar("Recusar oferta", "document.querySelector('dialog')");
  await p.aguardarTexto("Frete de R$ 7.500,00 (SP → PR) recusado.");
  await p.aguardarTexto("Nenhuma oferta aguardando aprovação.");
  ok("oferta recusada; fila vazia");

  const recusada = await fetch(`${BASE}/ofertas/${recusar.oferta.id}/aceite`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: recusar.token_aceite, cnpj: CNPJ_EMBARC, nome: "Maria",
                           email: "maria@exemplo.com" }) });
  if (recusada.status !== 400) falhar("oferta recusada não deveria poder ser aceita");
  ok("embarcador não consegue aceitar a oferta recusada");

  console.log("7. Embarcador aceita; histórico com link para /verificar");
  const cps = [await aceitar(aprovar), await aceitar(dentro)];
  await p.menu("/historico");
  await p.aguardar("document.querySelectorAll('article.frete').length === 2");
  const links = await p.avaliar(`[...document.querySelectorAll("article.frete a")].map((a) => a.getAttribute("href"))`);
  // linha do tempo do frete aprovado: pedido, oferta, aprovação, aceite, em ordem e em Brasília
  const tempo = await p.avaliar(`[...document.querySelectorAll(
    'article.frete[data-oferta="${aprovar.oferta.id}"] .lt-etapa')].map((li) => ({
      titulo: li.querySelector(".lt-titulo").textContent,
      quando: li.querySelector("time").textContent, em: li.querySelector("time").getAttribute("datetime"),
      texto: li.innerText }))`);
  if (tempo.map((e) => e.titulo).join("|") !== "Pedido recebido|Oferta do agente|Aprovada|Aceita pelo embarcador") {
    falhar(`linha do tempo errada: ${JSON.stringify(tempo)}`);
  }
  if (!tempo.every((e) => /^\d{2}\/\d{2}\/\d{4} às \d{2}:\d{2}$/.test(e.quando))) {
    falhar(`horário fora do formato de Brasília: ${JSON.stringify(tempo)}`);
  }
  if (tempo.map((e) => Date.parse(e.em)).some((t, i, a) => i && t < a[i - 1])) falhar("etapas fora de ordem");
  if (!tempo[1].texto.includes("Fora do mandato") || !tempo[2].texto.includes("Ana Gestora")
      || !tempo[3].texto.includes("Maria")) {
    falhar(`linha do tempo sem quem fez o quê: ${JSON.stringify(tempo)}`);
  }
  await p.clicar("Recusados");
  await p.aguardar(`document.querySelector('article.frete[data-oferta="${recusar.oferta.id}"]')`);
  await p.aguardarTexto("Motivo: Valor acima da tabela do cliente");
  await p.capturar("historico-recusados");
  await p.clicar("Fechados");
  await p.aguardar("document.querySelectorAll('article.frete').length === 2");
  for (const cp of cps) {
    if (!links.includes(`/verificar/${cp.id}`)) falhar(`sem link para ${cp.id}`);
  }
  await p.capturar("historico");
  for (const cp of cps) {
    const rel = await http("GET", `/compromissos/${cp.id}/verificacao`);
    if (!rel.valido || rel.verificacoes.length !== 15) falhar(`compromisso ${cp.id} inválido`);
  }
  await p.ir(`${BASE}/verificar/${cps[0].id}`);
  await p.aguardarTexto("COMPROMISSO VÁLIDO");
  await p.aguardar("document.querySelector('.qr img')?.complete && document.fonts.status === 'loaded'");
  const qr = await p.avaliar(`({ largura: document.querySelector(".qr img").naturalWidth,
    endereco: document.querySelector(".qr .endereco").textContent, aqui: location.href,
    css: getComputedStyle(document.querySelector(".topo")).display,
    fonte: getComputedStyle(document.body).fontFamily })`);
  if (!qr.largura || qr.endereco !== qr.aqui) falhar(`QR Code: ${JSON.stringify(qr)}`);
  // o roteiro roda em 127.0.0.1: a página tem de avisar que o QR só abre aqui
  if (!(await p.avaliar("document.querySelector('.aviso-local')?.textContent.includes('só abre neste computador')"))) {
    falhar("sem o aviso de endereço local embaixo do QR Code");
  }
  if (qr.css !== "grid" || !qr.fonte.startsWith('"Inter"') && !qr.fonte.startsWith("Inter")) {
    falhar(`estilo da página de verificação não carregou (CSP?): ${JSON.stringify(qr)}`);
  }
  await p.capturar("verificacao");
  await p.cdp("Emulation.setEmulatedMedia", { media: "print" });
  const impressao = await p.avaliar(`({
    botao: getComputedStyle(document.querySelector(".imprimir")).display,
    qr: getComputedStyle(document.querySelector(".qr img")).display })`);
  if (impressao.botao !== "none" || impressao.qr === "none") {
    falhar(`versão de impressão: ${JSON.stringify(impressao)}`);
  }
  await p.capturar("verificacao-impressao");
  await p.cdp("Emulation.setEmulatedMedia", { media: "" });
  await p.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await esperar(200);
  const estreita = await p.avaliar("document.documentElement.scrollWidth");
  if (estreita > 390) falhar(`/verificar rola para os lados em 390 px (${estreita} px)`);
  await p.capturar("celular-verificacao");
  await p.cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await p.ir(`${BASE}/painel/#/mandatos`);
  ok("2 compromissos, links certos, 15 de 15 checagens OK");
  ok("histórico: linha do tempo pedido → oferta → aprovação → aceite em Brasília; filtro de recusados");
  ok("/verificar: CSP sem nada inline, QR Code apontando para a própria página, impressão sem o botão, 390 px");

  console.log("8. Revogação de mandato");
  await p.aguardarTexto("Revogar mandato");
  await p.clicar("Revogar mandato");
  await p.aguardar("document.querySelector('dialog').open");
  await p.capturar("dialogo-revogacao");
  await p.clicar("Confirmar revogação", "document.querySelector('dialog')");
  await p.aguardar("document.querySelector('.etiqueta.ruim')?.textContent === 'Revogado'");
  await p.capturar("mandato-revogado");
  const depois = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 100_000, LOTACAO_500KM);
  if (!depois.violacoes.some((v) => v.startsWith("abaixo do piso mínimo da ANTT: risco de multa e indenização"))) {
    falhar(`a oferta de R$ 1.000 em 500 km deveria estar abaixo do piso: ${depois.violacoes}`);
  }
  if (depois.status !== "aguardando_aprovacao") falhar("oferta após revogação deveria ir para aprovação");
  ok("mandato revogado; nova oferta do agente cai na fila");

  console.log("9. Sair e entrar de novo com a chave guardada");
  await p.clicar("Sair");
  await p.aguardarTexto("Transportadora Fictícia Ltda");
  await p.aguardar("location.hash === '#/entrar'");
  const antigo = await fetch(`${BASE}/agentes`, { headers: { Authorization: `Bearer ${sessaoToken}` } });
  if (antigo.status !== 401) falhar("token antigo deveria ter sido encerrado");
  await p.capturar("entrar-com-chave");
  await p.clicar("Entrar");
  await p.aguardar("location.hash === '#/visao-geral'");
  ok("saiu (token encerrado) e entrou de novo assinando o desafio; abre na Visão geral");
  for (const trecho of ["Fretes fechados no mês", "R$ 13.850,00", "2 fretes, todos com prova verificável",
    "Aguardando sua decisão", "R$ 1.000,00 em ofertas", "Fretes barrados abaixo do piso",
    "Iriam sair R$ 3.071,76 abaixo do mínimo da ANTT. Não saíram sem a sua decisão.",
    "R$ 3.071,76 abaixo do piso da ANTT", "Comercial Exemplo de Alimentos Ltda (fictícia)",
    "Fretes fechados por embarcador"]) {
    await p.aguardarTexto(trecho);
  }
  const linhaPendente = await p.avaliar(`(() => {
    const l = document.querySelector("a.vg-linha");
    return { rota: l.querySelector(".vg-rota").textContent,
             embarcador: l.querySelector(".vg-embarcador").textContent };
  })()`);
  if (linhaPendente.rota !== "SP → PR"
      || linhaPendente.embarcador !== "Comercial Exemplo de Alimentos Ltda (fictícia)") {
    falhar(`linha aguardando: ${JSON.stringify(linhaPendente)}`);
  }
  await p.capturar("visao-geral");
  ok("Visão geral: fechados no mês, aguardando, barrados abaixo do piso e por embarcador");
  await p.menu("/aprovacoes");
  await p.aguardar("document.querySelectorAll('article.cartao').length === 1");
  await p.aguardarTexto("2 fretes já fechados com ele");
  ok("depois de 2 aceites, a oferta nova mostra '2 fretes já fechados com ele'");
  await p.aguardarTexto("R$ 3.071,76 abaixo do piso mínimo da ANTT: risco de multa e indenização.");
  if (await p.avaliar("document.querySelector('main').innerText.includes('(piso de R$ 4.071,76')")) {
    falhar("a frase do piso aparece repetida no cartão");
  }
  await p.capturar("abaixo-do-piso");
  ok("oferta de R$ 1.000 em 500 km: motivo 'abaixo do piso' com os números, sem repetir");

  // aprovar abaixo do piso exige justificativa escrita, assinada com a aprovação
  await p.clicar("Aprovar");
  await p.aguardar("document.querySelector('dialog').open");
  await p.aguardarTexto("Justificativa (obrigatória: frete abaixo do piso da ANTT)");
  await p.preencher("dialog input", "Ana Gestora");
  await p.clicar("Aprovar oferta", "document.querySelector('dialog')");
  await esperar(300);
  if (!(await p.avaliar("document.querySelector('dialog').open"))) {
    falhar("aprovou abaixo do piso sem justificativa");
  }
  const justificativa = "Cliente estratégico; diferença coberta pelo contrato anual.";
  await p.preencher("dialog textarea[name=justificativa]", justificativa);
  await p.capturar("justificativa-abaixo-do-piso");
  await p.clicar("Aprovar oferta", "document.querySelector('dialog')");
  await p.aguardarTexto("Frete de R$ 1.000,00 (SP → PR) aprovado e enviado ao embarcador.");
  const aprovada = (await comToken(await tokenDa(p), "/ofertas"))
    .find((o) => o.oferta.id === depois.oferta.id);
  if (aprovada.aprovacao?.justificativa !== justificativa || !aprovada.aprovacao?.assinatura) {
    falhar(`justificativa não gravada na aprovação: ${JSON.stringify(aprovada.aprovacao)}`);
  }
  ok("aprovar abaixo do piso: sem justificativa o diálogo não fecha; com ela, vai assinada");
  await p.menu("/seguranca");
  await p.aguardarTexto("Histórico de acesso");
  await p.aguardarTexto("Criado em");
  const codigos = await p.avaliar(
    "document.querySelector('main').innerText.match(/org-\\d+#|recuperacao-\\d+#|Registro nº|#\\d+\\b/g)");
  if (codigos) falhar(`códigos técnicos à vista na aba Segurança: ${codigos}`);
  await p.capturar("seguranca");
  await p.avaliar("document.querySelectorAll('details').forEach((d) => { d.open = true; })");
  await p.aguardarTexto("Identificador do acesso");
  await p.aguardarTexto("Registro nº");
  ok("aba Segurança: datas em vez de códigos; códigos só em 'Ver detalhes técnicos'");
  await emitirMandatoPelaTela(p);
  const [{ mandato: { id: mandato2Id } }] = (await comToken(await tokenDa(p), "/mandatos"))
    .filter((m) => !m.revogado_em);
  ok(`segundo mandato ativo (${mandato2Id}), para a recuperação revogar`);

  console.log("10. Recuperação em outro computador");
  const { browserContextId } = await navegador.cdp("Target.createBrowserContext");
  await navegador.cdp("Browser.setDownloadBehavior",
    { behavior: "allow", downloadPath: DOWNLOADS2, browserContextId });
  const { targetId } = await navegador.cdp("Target.createTarget",
    { url: "about:blank", browserContextId });
  const p2 = await Pagina.abrir(targetId);
  await p2.ir(`${BASE}/painel/`);
  await p2.aguardarTexto("Nenhuma transportadora tem acesso guardado neste computador.");
  await p2.clicar("Recuperar acesso com o arquivo");
  await p2.aguardarTexto("1. Abrir o arquivo de recuperação");
  await p2.escolherArquivo("input[type=file]", arquivo1);
  await p2.preencher("input[name=frase]", "ABCDE-FGHJK-MNPQR-STVWX-YZ012");
  await p2.clicar("Abrir arquivo");
  await p2.aguardarTexto(
    "Não foi possível abrir o arquivo: a frase-senha está errada ou o arquivo foi alterado");
  await p2.capturar("recuperar-frase-errada");
  ok("frase errada: mensagem clara na tela, nada muda");

  await p2.preencher("input[name=frase]", frase1.toLowerCase());
  await p2.clicar("Abrir arquivo");
  await p2.aguardar("document.querySelector('.frase-senha')", 15000);
  const frase2 = await p2.avaliar("document.querySelector('.frase-senha').textContent");
  if (frase2 === frase1) falhar("a nova frase deveria ser outra");
  await guardarRecuperacao(p2, DOWNLOADS2, frase2, false);
  await p2.aguardar("document.querySelector('input[name=revogar]')?.offsetParent");
  if (!(await p2.avaliar("document.querySelector('input[name=revogar]').checked"))) {
    falhar("'Revogar todos os mandatos ativos' deveria vir marcado");
  }
  await p2.capturar("recuperar-trocar");
  await p2.clicar("Recuperar o acesso");
  await p2.aguardar("location.hash === '#/mandatos'", 20000);
  await p2.aguardar("document.querySelectorAll('.etiqueta').length === 2 && "
    + "[...document.querySelectorAll('.etiqueta')].every((e) => e.textContent === 'Revogado')");
  await p2.capturar("recuperado-mandatos-revogados");
  ok("chave trocada; 'revogar mandatos' veio marcado e os 2 mandatos estão revogados");

  const chave2 = await p2.avaliar(`new Promise((ok) => {
    indexedDB.open("elo-painel").onsuccess = (e) => {
      const g = e.target.result.transaction("chaves").objectStore("chaves").getAll();
      g.onsuccess = () => ok({ extraivel: g.result[0].privada.extractable,
                               publica: g.result[0].chavePublica });
    };
  })`);
  if (chave2.extraivel !== false || chave2.publica === chave.chavePublica) {
    falhar(`chave nova inesperada: ${JSON.stringify(chave2)}`);
  }
  const resumo = await comToken(await tokenDa(p2), "/organizacao");
  if (resumo.chave_atual.chave_publica !== chave2.publica
      || resumo.historico_chaves.map((e) => e.valido).join() !== "true,true") {
    falhar(`histórico inesperado: ${JSON.stringify(resumo.historico_chaves)}`);
  }
  ok("chave nova não exportável no outro computador; histórico: recuperação definida e "
     + "rotação, ambas OK");

  const fora = await emitirOferta(agente, pedido.id, agenteId, mandato2Id, 100_000);
  if (fora.status !== "aguardando_aprovacao" || !fora.violacoes.some((v) => v.includes("revogado"))) {
    falhar(`oferta com mandato revogado na recuperação: ${JSON.stringify(fora.violacoes)}`);
  }
  ok("oferta nova com o mandato revogado na recuperação sai do mandato (vai para aprovação)");
  for (const cp of cps) {
    const rel = await http("GET", `/compromissos/${cp.id}/verificacao`);
    if (!rel.valido || rel.verificacoes.length !== 15) falhar(`compromisso ${cp.id} inválido`);
  }
  ok("compromissos de antes da troca continuam com 15 de 15");

  console.log("11. A chave antiga não entra mais");
  await p.avaliar("sessionStorage.clear()");
  await p.ir(`${BASE}/painel/#/entrar`);
  await p.avaliar("location.reload()");
  await p.aguardar("document.readyState === 'complete'");
  await p.aguardarTexto("Transportadora Fictícia Ltda");
  await p.clicar("Entrar");
  await p.aguardarTexto("O acesso guardado neste computador não vale mais");
  await p.capturar("chave-antiga-recusada");
  if (await p.avaliar("location.hash") !== "#/entrar") falhar("a chave antiga entrou");
  // o 401 de POST /sessoes é o esperado aqui
  p.problemas = p.problemas.filter((x) => !(x.includes("status of 401") && x.includes("/sessoes")));
  ok("no primeiro computador, a chave antiga é recusada com mensagem clara");

  console.log("12. Modo demonstração");
  const botaoDemo = "[...document.querySelectorAll('button')].find((b) => "
    + "b.textContent.trim() === 'Simular pedido de cotação')";
  await p2.aguardar(botaoDemo);
  await p2.clicar("Simular pedido de cotação");
  await p2.aguardar("document.querySelector('dialog').open");
  await p2.aguardarTexto("Preparar demonstração");
  await p2.capturar("preparar-demonstracao");
  await p2.clicar("Assinar e preparar", "document.querySelector('dialog')");
  await p2.aguardarTexto("Pedido simulado de Comercial Exemplo de Alimentos Ltda (fictícia): 3 ofertas. "
    + "1 foi direto ao embarcador e 2 aguardam a sua decisão.", 20000);
  await p2.aguardar("location.hash === '#/aprovacoes'");
  await p2.aguardarTexto("Valor R$ 9.000,00 acima do limite de R$ 6.000,00");
  await p2.aguardarTexto("R$ 1.857,20 abaixo do piso mínimo da ANTT: risco de multa e indenização.");
  await p2.capturar("demonstracao-aprovacoes");
  const cartoesAntes = await p2.avaliar("document.querySelectorAll('article.cartao').length");
  await p2.clicar("Simular pedido de cotação");
  await p2.aguardar(`document.querySelectorAll('article.cartao').length === ${cartoesAntes + 2}`, 20000);
  if (await p2.avaliar("document.querySelector('dialog').open")) falhar("pediu para preparar de novo");
  ok("demonstração: prepara uma vez (assinando no navegador) e simula 3 ofertas por pedido");
  await percorrerTelas(p2, 1280, 900, "desktop");
  ok("desktop (1280 px): topo numa linha, menu numa linha, botão de demonstração, sem rolagem lateral");
  await percorrerTelas(p2, 390, 844, "celular");
  await p2.cdp("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  ok("celular (390 px): todas as telas sem rolagem lateral, menu numa linha, sem botão de demonstração");

  console.log("13. Conferências finais");
  const corpos = [...p.corpos, ...p2.corpos];
  const comPrivada = corpos.filter(({ corpo }) =>
    /privad|private|pkcs8|"d"\s*:|"jwk"|"cifra"|"kdf"/i.test(corpo));
  if (comPrivada.length) falhar(`corpo suspeito enviado: ${JSON.stringify(comPrivada)}`);
  if (corpos.some(({ corpo }) => corpo.includes(frase1) || corpo.includes(frase2)
      || corpo.includes(frase1.replaceAll("-", "")))) {
    falhar("uma frase-senha foi enviada ao servidor");
  }
  const publicaEnviada = corpos.some(({ corpo }) => corpo.includes(chave.chavePublica));
  if (!publicaEnviada) falhar("a chave pública deveria ter ido no cadastro");
  ok(`${corpos.length} requisições com corpo: nenhuma com chave privada, arquivo ou frase`);
  const problemas = [...p.problemas, ...p2.problemas];
  if (problemas.length) falhar(`erros no navegador:\n${problemas.join("\n")}`);
  ok("nenhum erro de console, exceção ou violação de CSP");
}

let codigo = 0;
try {
  await roteiro();
  console.log("\nTudo certo.");
} catch (erro) {
  console.error(`\nFALHOU: ${erro.message}`);
  codigo = 1;
} finally {
  for (const p of processos) p.kill();
  await esperar(500);
  try { rmSync(temporario, { recursive: true, force: true }); } catch { /* Chrome ainda soltando */ }
}
process.exit(codigo);
