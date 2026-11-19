// Tela inicial "Visão geral": números do mês e o que espera decisão.

import * as api from "./api.js";
import { h } from "./dom.js";
import { datasEmBrasilia, reais } from "./formatos.js";

const MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
  "setembro", "outubro", "novembro", "dezembro"];
const AVISO_PISO = "abaixo do piso mínimo da ANTT";
const PISO_NAO_CONFERIDO = "não foi possível conferir o piso da ANTT";

/** [status, texto] do motivo principal: risco (vermelho) ou atenção (âmbar). */
function motivoPrincipal(item) {
  const v = item.violacoes;
  const p = item.frete?.piso;
  if (v.some((x) => x.startsWith(AVISO_PISO)) && p && !p.estimado) {
    return ["risco", `${reais(-p.folga_centavos)} abaixo do piso da ANTT`];
  }
  if (v.some((x) => x.includes(PISO_NAO_CONFERIDO))) return ["atencao", "Piso da ANTT não conferido"];
  if (v.some((x) => x.includes("acima do limite"))) return ["atencao", "Acima do limite do mandato"];
  if (v.some((x) => x.startsWith("mandato revogado"))) return ["atencao", "Mandato revogado"];
  const texto = datasEmBrasilia(v[0] ?? "Aguardando decisão");
  return ["atencao", texto.charAt(0).toUpperCase() + texto.slice(1)];
}

// só as UFs, numa linha (os CEPs estão no cartão de Aprovações)
const rota = (c) => `${c.origem_uf} → ${c.destino_uf}`;

function numero(rotulo, grande, detalhe, status) {
  return h("div", { class: "vg-numero" },
    h("div", { class: "vg-rotulo" }, rotulo),
    h("div", { class: `vg-grande ${status ?? ""}` }, grande),
    h("div", { class: "vg-detalhe" }, detalhe));
}

export async function telaVisaoGeral() {
  const v = await api.obter("/visao-geral");
  const [ano, mes] = v.mes.split("-");
  const { fechados, aguardando, barradas_piso: barradas } = v;

  const pendentes = aguardando.itens.length === 0
    ? h("p", { class: "vg-vazio" }, "Nada esperando decisão agora.")
    : aguardando.itens.map((item) => {
      const [status, texto] = motivoPrincipal(item);
      const o = item.oferta;
      return h("a", { class: "vg-linha", href: "#/aprovacoes" },
        h("span", { class: "vg-valor" }, reais(o.condicoes.valor_centavos)),
        h("span", { class: "vg-quem" },
          h("span", { class: "vg-rota" }, rota(o.carga)),
          h("span", { class: "vg-embarcador" }, item.embarcador.nome ?? item.embarcador.cnpj)),
        h("span", { class: `vg-status ${status}` }, texto));
    });

  const embarcadores = v.embarcadores.length === 0
    ? h("p", { class: "vg-vazio" }, "Nenhum frete fechado ainda neste mês.")
    : v.embarcadores.map((e) => h("div", { class: "vg-linha vg-embarcadores" },
      h("span", { class: "vg-quem" }, h("span", { class: "vg-rota" }, e.nome ?? e.cnpj)),
      h("span", { class: "vg-valor" }, `${e.fretes} frete${e.fretes > 1 ? "s" : ""}`),
      h("span", { class: "vg-total" }, reais(e.total_centavos))));

  return h("section", { class: "visao-geral" },
    h("div", { class: "vg-mes" },
      `${api.sessao()?.razaoSocial ?? ""} · ${MESES[Number(mes) - 1]} de ${ano}`),
    h("h1", { tabindex: -1 }, "Visão geral"),
    h("div", { class: "vg-numeros" },
      numero("Fretes fechados no mês", reais(fechados.total_centavos),
        fechados.quantidade === 0 ? "Nenhum frete fechado ainda neste mês."
          : `${fechados.quantidade} frete${fechados.quantidade > 1 ? "s" : ""}, todos com prova verificável`),
      numero("Aguardando sua decisão", String(aguardando.quantidade),
        `${reais(aguardando.total_centavos)} em ofertas`, aguardando.quantidade ? "atencao" : ""),
      numero("Fretes barrados abaixo do piso", String(barradas.quantidade),
        barradas.quantidade
          ? `Iriam sair ${reais(barradas.abaixo_do_minimo_centavos)} abaixo do mínimo da ANTT. `
            + "Não saíram sem a sua decisão."
          : "Nenhum frete abaixo do piso neste mês.",
        barradas.quantidade ? "risco" : "")),
    h("div", { class: "vg-duas" },
      h("section", { class: "vg-secao" }, h("h2", {}, "Aguardando sua decisão"), pendentes),
      h("section", { class: "vg-secao" }, h("h2", {}, "Fretes fechados por embarcador"),
        embarcadores)));
}
