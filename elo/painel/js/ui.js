// Componentes de tela compartilhados: avisos, diálogo de confirmação e
// pedaços de formulário. Tudo montado com dom.js (sem interpretar HTML).

import { h, limpar } from "./dom.js";

const avisos = document.getElementById("avisos");
const dialogo = document.getElementById("dialogo");

// ---------- avisos e diálogos ----------

const prazos = new WeakMap(); // aviso -> timer que o remove

export function avisar(texto, tipo = "ok") {
  const duracao = tipo === "ok" ? 5000 : 10000;
  // a mesma mensagem de novo não empilha: o aviso que já está na tela ganha a
  // contagem ("2 vezes") e o prazo para sumir recomeça
  const igual = [...avisos.children].find((a) => a.dataset.texto === texto && a.dataset.tipo === tipo);
  if (igual) {
    const vezes = Number(igual.dataset.vezes) + 1;
    igual.dataset.vezes = String(vezes);
    igual.querySelector(".vezes").textContent = ` (${vezes} vezes)`;
    clearTimeout(prazos.get(igual));
    prazos.set(igual, setTimeout(() => igual.remove(), duracao));
    avisos.append(igual); // a mais recente fica embaixo
    return;
  }
  const aviso = h("div", { class: `aviso aviso-${tipo}`, role: tipo === "erro" ? "alert" : "status",
                           "data-texto": texto, "data-tipo": tipo, "data-vezes": "1" },
    h("span", {}, texto, h("span", { class: "vezes" })),
    h("button", { class: "fechar", type: "button", "aria-label": "Fechar",
                  onclick: () => aviso.remove() }, "×"));
  avisos.append(aviso);
  while (avisos.children.length > 3) avisos.firstElementChild.remove();
  // a faixa de avisos fica no fluxo da página (empurra o conteúdo, nunca cobre
  // um botão); se ela estiver fora da tela, rola até ela
  if (avisos.getBoundingClientRect().bottom < 0) avisos.scrollIntoView({ behavior: "smooth" });
  prazos.set(aviso, setTimeout(() => aviso.remove(), duracao));
}

export function avisarErro(erro) {
  console.error(erro);
  avisar(erro?.message ?? String(erro), "erro");
}

/**
 * Abre um diálogo de confirmação. `campos` são elementos extras (ex.: motivo).
 * Resolve true se a pessoa confirmar.
 */
export function confirmar({ titulo, corpo, botao, perigo = false, campos = [],
                            cancelar = "Cancelar" }) {
  return new Promise((resolve) => {
    const form = h("form", { method: "dialog", class: "dialogo-corpo" },
      h("h2", {}, titulo),
      ...[corpo].flat().map((p) => (p instanceof Node ? p : h("p", {}, p))),
      ...campos,
      h("div", { class: "acoes" },
        h("button", { type: "button", class: "botao secundario",
                      onclick: () => dialogo.close("nao") }, cancelar),
        h("button", { type: "submit", value: "sim",
                      class: `botao ${perigo ? "perigo" : "primario"}` }, botao)));
    limpar(dialogo).append(form);
    dialogo.addEventListener("close", () => resolve(dialogo.returnValue === "sim"),
                             { once: true });
    dialogo.returnValue = "";
    dialogo.showModal();
  });
}

/** Desabilita o botão enquanto a ação roda, para não registrar duas vezes. */
export async function executando(botao, acao) {
  const texto = botao.textContent;
  botao.disabled = true;
  botao.textContent = "Registrando…";
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
