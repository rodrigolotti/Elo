// Verificação de ponta a ponta do painel num Chrome de verdade (headless).
//
//   node tests/e2e/painel_chrome.mjs [pasta-das-capturas]
//
// Sobe a API com um banco temporário, abre o Chrome com um perfil vazio e
// percorre as telas como uma pessoa: cadastro, agente, mandato, aprovação,
// recusa, histórico, revogação, sair e entrar de novo. Este script faz o papel
// do agente (emite ofertas) e do embarcador (aceita pelo link).
//
// Confere também: chave no IndexedDB não exportável, nenhum erro de console
// ou de CSP, e nenhum corpo enviado ao servidor com chave privada.
//
// Variáveis: CHROME (caminho do navegador), PYTHON (padrão: python).

import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assinar, chavePublicaBase64, gerarParDeChaves } from "../../elo/painel/js/assinatura.js";

const PORTA_API = 8765 + Math.floor(Math.random() * 500);
const PORTA_CDP = 9333 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORTA_API}`;
const CAPTURAS = process.argv[2] ?? join(tmpdir(), "elo-e2e-capturas");
const temporario = mkdtempSync(join(tmpdir(), "elo-e2e-"));
mkdirSync(CAPTURAS, { recursive: true });

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

  static async abrir() {
    const [alvo] = (await (await fetch(`http://127.0.0.1:${PORTA_CDP}/json/list`)).json())
      .filter((t) => t.type === "page");
    const ws = new WebSocket(alvo.webSocketDebuggerUrl);
    await new Promise((r, e) => { ws.onopen = r; ws.onerror = e; });
    const pagina = new Pagina(ws);
    for (const dominio of ["Page", "Runtime", "Log", "Network"]) await pagina.cdp(`${dominio}.enable`);
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

  // clica num item do menu pelo endereço (o texto pode ter o contador junto)
  menu(rota) {
    return this.avaliar(`document.querySelector('nav a[href="#${rota}"]').click()`);
  }

  async capturar(nome) {
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

async function emitirOferta(agente, pedidoId, agenteId, mandatoId, valor) {
  const { rascunho } = await http("POST", "/ofertas/rascunho", {
    pedido_id: pedidoId, agente_id: agenteId, mandato_id: mandatoId,
    condicoes: { valor_centavos: valor, coleta_ate: emBrasiliaDaqui(2 * 864e5).slice(0, 10),
                 prazo_entrega_dias_uteis: 2, inclui: ["pedágio", "seguro"] },
    valida_ate: emBrasiliaDaqui(864e5),
  });
  return http("POST", "/ofertas", await assinar(rascunho, agente.privateKey, "agente-cotacao#k1"));
}

const aceitar = (oferta) => http("POST", `/ofertas/${oferta.oferta.id}/aceite`, {
  token: oferta.token_aceite, cnpj: CNPJ_EMBARC, nome: "Maria Exemplo",
  email: "maria@exemplo.com",
});

// ---------- roteiro ----------

async function roteiro() {
  console.log(`API em ${BASE}; capturas em ${CAPTURAS}`);
  subir(process.env.PYTHON ?? "python", ["-m", "uvicorn", "elo.api:app", "--port",
    String(PORTA_API)], { env: { ...process.env, ELO_DB: join(temporario, "elo.db") } });
  await aguardarUrl(`${BASE}/openapi.json`);
  if (!CHROME) falhar("Chrome/Edge não encontrado: defina CHROME");
  subir(CHROME, ["--headless=new", `--remote-debugging-port=${PORTA_CDP}`,
    `--user-data-dir=${join(temporario, "perfil")}`, "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "about:blank"]);
  await aguardarUrl(`http://127.0.0.1:${PORTA_CDP}/json/version`);
  const p = await Pagina.abrir();

  console.log("1. Entrar sem chave no navegador");
  await p.ir(`${BASE}/painel/`);
  await p.aguardarTexto("Nenhuma organização tem chave neste navegador.");
  await p.capturar("entrar-vazio");
  ok("tela de entrada sem organização");

  console.log("2. Cadastro da organização");
  await p.clicar("Cadastrar organização");
  await p.aguardarTexto("Atenção: a chave da organização fica só neste navegador");
  await p.preencher("input[name=cnpj]", CNPJ_TRANSP);
  await p.preencher("input[name=razao_social]", "Transportadora Fictícia Ltda");
  await p.capturar("cadastro");
  await p.clicar("Gerar chave e cadastrar");
  const bloqueado = await p.avaliar("location.hash");
  if (bloqueado !== "#/cadastro") falhar("enviou sem marcar o 'Entendi'");
  await p.avaliar(`document.querySelector(".confirmacao input").checked = true`);
  await p.clicar("Gerar chave e cadastrar");
  await p.aguardar("location.hash === '#/agentes'");
  await p.aguardarTexto("Transportadora Fictícia Ltda");
  ok("organização cadastrada, sessão aberta");

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
  await p.clicar("Assinar certificado");
  await p.aguardarTexto("32 bytes em base64");
  await p.preencher("form textarea", pubAgente);
  await p.clicar("Assinar certificado");
  await p.aguardarTexto("Agente Agente de cotação criado");
  await p.aguardar("document.querySelectorAll('tbody tr').length === 1");
  await p.capturar("agentes");
  ok("chave inválida recusada no formulário; agente criado");

  console.log("4. Definição de mandato");
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
  await p.preencher("form textarea", "carga geral");
  await p.capturar("mandato-formulario");
  await p.clicar("Assinar mandato");
  await p.aguardarTexto("emitido.");
  await p.aguardar("document.querySelector('.etiqueta.bom')?.textContent === 'Ativo'");
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
    carga: { origem_cep: "01000-000", origem_uf: "SP", destino_cep: "80000-000",
             destino_uf: "PR", peso_kg: 3200, volume_litros: 14500, tipo: "carga geral" } });
  const aprovar = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 900_000);
  const recusar = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 750_000);
  const dentro = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 485_000);
  if (aprovar.status !== "aguardando_aprovacao" || dentro.status !== "enviada") {
    falhar("status das ofertas inesperado");
  }

  console.log("6. Fila de aprovação: aprovar e recusar");
  await p.menu("/aprovacoes");
  await p.aguardar("document.querySelectorAll('article.cartao').length === 2");
  await p.aguardar("document.querySelector('nav .contador')?.textContent === '2'");
  await p.capturar("fila-aprovacao");
  ok("duas ofertas na fila, com contador no menu");

  const cartao = (id) => `[...document.querySelectorAll("article.cartao")].find((c) => c.innerText.includes(${JSON.stringify(id)}))`;
  await p.clicar("Aprovar", cartao(aprovar.oferta.id));
  await p.aguardar("document.querySelector('dialog').open");
  await p.preencher("dialog input", "Ana Gestora");
  await p.capturar("dialogo-aprovacao");
  await p.clicar("Assinar aprovação", "document.querySelector('dialog')");
  await p.aguardarTexto(`Oferta ${aprovar.oferta.id} aprovada.`);
  await p.aguardar("document.querySelectorAll('article.cartao').length === 1");
  ok("oferta aprovada com assinatura do navegador");

  await p.clicar("Recusar", cartao(recusar.oferta.id));
  await p.aguardar("document.querySelector('dialog').open");
  await p.avaliar(`document.querySelector("dialog input").value`).then((v) => {
    if (v !== "Ana Gestora") falhar("o nome do responsável não foi lembrado");
  });
  await p.preencher("dialog textarea", "Valor acima da tabela do cliente");
  await p.capturar("dialogo-recusa");
  await p.clicar("Assinar recusa", "document.querySelector('dialog')");
  await p.aguardarTexto(`Oferta ${recusar.oferta.id} recusada.`);
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
  await p.aguardar("document.querySelectorAll('tbody tr').length === 2");
  const links = await p.avaliar(`[...document.querySelectorAll("tbody a")].map((a) => a.getAttribute("href"))`);
  for (const cp of cps) {
    if (!links.includes(`/verificar/${cp.id}`)) falhar(`sem link para ${cp.id}`);
  }
  await p.capturar("historico");
  for (const cp of cps) {
    const rel = await http("GET", `/compromissos/${cp.id}/verificacao`);
    if (!rel.valido || rel.verificacoes.length !== 14) falhar(`compromisso ${cp.id} inválido`);
  }
  await p.ir(`${BASE}/verificar/${cps[0].id}`);
  await p.aguardarTexto("COMPROMISSO VÁLIDO");
  await p.capturar("verificacao");
  await p.ir(`${BASE}/painel/#/mandatos`);
  ok("2 compromissos, links certos, 14 de 14 checagens OK");

  console.log("8. Revogação de mandato");
  await p.aguardarTexto("Revogar mandato");
  await p.clicar("Revogar mandato");
  await p.aguardar("document.querySelector('dialog').open");
  await p.capturar("dialogo-revogacao");
  await p.clicar("Assinar revogação", "document.querySelector('dialog')");
  await p.aguardar("document.querySelector('.etiqueta.ruim')?.textContent === 'Revogado'");
  await p.capturar("mandato-revogado");
  const depois = await emitirOferta(agente, pedido.id, agenteId, mandatoId, 100_000);
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
  await p.aguardar("location.hash === '#/aprovacoes'");
  await p.aguardar("document.querySelectorAll('article.cartao').length === 1");
  ok("saiu (token encerrado) e entrou de novo assinando o desafio");

  console.log("10. Conferências finais");
  const comPrivada = p.corpos.filter(({ corpo }) =>
    /privad|private|pkcs8|"d"\s*:|"jwk"/i.test(corpo));
  if (comPrivada.length) falhar(`corpo suspeito enviado: ${JSON.stringify(comPrivada)}`);
  const publicaEnviada = p.corpos.some(({ corpo }) => corpo.includes(chave.chavePublica));
  if (!publicaEnviada) falhar("a chave pública deveria ter ido no cadastro");
  ok(`${p.corpos.length} requisições com corpo: nenhuma com chave privada`);
  if (p.problemas.length) falhar(`erros no navegador:\n${p.problemas.join("\n")}`);
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
