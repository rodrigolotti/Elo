// Componentes de tela compartilhados: avisos, diálogo de confirmação e
// pedaços de formulário. Tudo montado com dom.js (sem interpretar HTML).

import { h, limpar } from "./dom.js";

const avisos = document.getElementById("avisos");
const dialogo = document.getElementById("dialogo");

// ---------- avisos e diálogos ----------

export function avisar(texto, tipo = "ok") {
  const aviso = h("div", { class: `aviso aviso-${tipo}`, role: tipo === "erro" ? "alert" : "status" },
    h("span", {}, texto),
    h("button", { class: "fechar", type: "button", "aria-label": "Fechar",
                  onclick: () => aviso.remove() }, "×"));
  avisos.append(aviso);
  while (avisos.children.length > 3) avisos.firstElementChild.remove();
  setTimeout(() => aviso.remove(), tipo === "ok" ? 5000 : 10000);
}

export function avisarErro(erro) {
  console.error(erro);
  avisar(erro?.message ?? String(erro), "erro");
}

/**
 * Abre um diálogo de confirmação. `campos` são elementos extras (ex.: motivo).
 * Resolve true se a pessoa confirmar.
 */
export function confirmar({ titulo, corpo, botao, perigo = false, campos = [] }) {
  return new Promise((resolve) => {
    const form = h("form", { method: "dialog", class: "dialogo-corpo" },
      h("h2", {}, titulo),
      ...[corpo].flat().map((p) => (p instanceof Node ? p : h("p", {}, p))),
      ...campos,
      h("div", { class: "acoes" },
        h("button", { type: "button", class: "botao secundario",
                      onclick: () => dialogo.close("nao") }, "Cancelar"),
        h("button", { type: "submit", value: "sim",
                      class: `botao ${perigo ? "perigo" : "primario"}` }, botao)));
    limpar(dialogo).append(form);
    dialogo.addEventListener("close", () => resolve(dialogo.returnValue === "sim"),
                             { once: true });
    dialogo.returnValue = "";
    dialogo.showModal();
  });
}

/** Desabilita o botão enquanto a ação roda, para não assinar duas vezes. */
export async function executando(botao, acao) {
  const texto = botao.textContent;
  botao.disabled = true;
  botao.textContent = "Assinando…";
  try {
    return await acao();
  } finally {
    botao.disabled = false;
    botao.textContent = texto;
  }
}

// ---------- pedaços reutilizados ----------

export const campo = (rotulo, controle, ajuda) =>
  h("label", { class: "campo" }, h("span", { class: "rotulo" }, rotulo), controle,
    ajuda ? h("span", { class: "ajuda" }, ajuda) : null);

export const dados = (pares) => h("dl", { class: "dados" },
  pares.filter(Boolean).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

export const mono = (texto) => h("span", { class: "mono" }, texto);

export const vazio = (texto, ...extra) => h("div", { class: "vazio" }, h("p", {}, texto), ...extra);

export function cabecalho(titulo, subtitulo) {
  return h("div", { class: "cabecalho" }, h("h1", { tabindex: -1 }, titulo),
    subtitulo ? h("p", { class: "subtitulo" }, subtitulo) : null);
}

/** Baixa um objeto como arquivo JSON (sem passar pelo servidor). */
export function baixarJson(nome, objeto) {
  const texto = `${JSON.stringify(objeto, null, 2)}\n`;
  const url = URL.createObjectURL(new Blob([texto], { type: "application/json" }));
  const link = h("a", { href: url, download: nome });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
