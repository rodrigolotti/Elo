// Histórico: cada frete com a linha do tempo tirada do registro
// (pedido, oferta do agente, aprovação ou recusa, aceite), no horário de Brasília.

import * as api from "./api.js";
import { h, limpar } from "./dom.js";
import { datasEmBrasilia, emBrasilia, reais, rota } from "./formatos.js";
import { avisarErro, cabecalho, vazio } from "./ui.js";

const FILTROS = [
  ["fechado", "Fechados"],
  ["em-aberto", "Em aberto"],
  ["recusado", "Recusados"],
];

const SITUACAO = {
  fechado: ["ok", "Fechado"],
  "em-aberto": ["atencao", "Em aberto"],
  recusado: ["risco", "Recusado"],
};

const maiuscula = (t) => t.charAt(0).toUpperCase() + t.slice(1);

/** [título, quem, detalhe] de cada etapa, na língua do transportador. */
function etapa(e) {
  switch (e.etapa) {
    case "pedido":
      return ["Pedido recebido", null, null];
    case "oferta":
      return ["Oferta do agente", e.por,
        e.detalhe === "dentro do mandato" ? "Dentro do mandato: saiu direto ao embarcador"
          : `Fora do mandato: ${datasEmBrasilia(e.detalhe)}`];
    case "aprovacao":
      return ["Aprovada", e.por, e.detalhe ? `Justificativa: ${e.detalhe}` : null];
    case "recusa":
      return ["Recusada", e.por, e.detalhe ? `Motivo: ${e.detalhe}` : null];
    case "aceite":
      return ["Aceita pelo embarcador", e.por, null];
    default:
      return [maiuscula(e.etapa), e.por, e.detalhe];
  }
}

function linhaDoTempo(tempo) {
  return h("ol", { class: "lt", "aria-label": "Linha do tempo (horário de Brasília)" },
    tempo.map((e) => {
      const [titulo, quem, detalhe] = etapa(e);
      return h("li", { class: `lt-etapa lt-${e.etapa}` },
        h("div", { class: "lt-titulo" }, titulo),
        h("time", { class: "lt-quando", datetime: e.em }, emBrasilia(e.em)),
        quem ? h("div", { class: "lt-quem" }, quem) : null,
        detalhe ? h("div", { class: "lt-detalhe" }, detalhe) : null);
    }));
}

function cartaoFrete(f) {
  const [status, rotulo] = SITUACAO[f.situacao];
  return h("article", { class: "cartao frete", "data-oferta": f.oferta.id },
    h("div", { class: "cartao-topo" },
      h("div", {},
        h("h2", { class: "c-valor" }, reais(f.oferta.condicoes.valor_centavos)),
        h("div", { class: "c-rota" }, rota(f.oferta.carga)),
        h("div", { class: "ajuda" }, f.embarcador?.nome ?? f.oferta.referencia?.embarcador_cnpj ?? "")),
      h("span", { class: `etiqueta-status ${status}` }, rotulo)),
    linhaDoTempo(f.linha_do_tempo),
    f.verificacao ? h("div", { class: "frete-rodape" },
      h("a", { href: f.verificacao, target: "_blank", rel: "noopener" },
        "Verificar o compromisso ↗")) : null);
}

export async function telaHistorico() {
  const lista = h("div", { class: "lista-cartoes", "aria-live": "polite" });
  const botoes = FILTROS.map(([valor, texto]) => h("button", {
    type: "button", class: "filtro", "data-situacao": valor, "aria-pressed": "false",
    onclick: () => mostrar(valor),
  }, texto));

  async function mostrar(situacao) {
    for (const b of botoes) b.setAttribute("aria-pressed", String(b.dataset.situacao === situacao));
    try {
      const fretes = await api.obter(`/fretes?situacao=${encodeURIComponent(situacao)}`);
      const nome = FILTROS.find(([v]) => v === situacao)[1].toLowerCase();
      limpar(lista).append(...(fretes.length ? fretes.map(cartaoFrete)
        : [vazio(`Nenhum frete ${nome.replace(/s$/, "")}.`)]));
    } catch (erro) {
      avisarErro(erro);
    }
  }

  await mostrar("fechado");
  return h("section", {},
    cabecalho("Histórico", "Cada frete com o que aconteceu, do pedido ao aceite, "
      + "no horário de Brasília. Os fechados têm prova que qualquer pessoa verifica."),
    h("div", { class: "filtros", role: "group", "aria-label": "Mostrar fretes" }, botoes),
    lista);
}
