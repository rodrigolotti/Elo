// Tela inicial "Visão geral": números do mês e o que espera decisão.

import * as api from "./api.js";
import { h } from "./dom.js";
import { contar, reais } from "./formatos.js";
import { motivosOrdenados } from "./motivos.js";

const MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
  "setembro", "outubro", "novembro", "dezembro"];
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
      const motivos = motivosOrdenados(item.violacoes, item.frete);
      const o = item.oferta;
      return h("a", { class: "vg-linha", href: "#/aprovacoes" },
        h("span", { class: "vg-valor" }, reais(o.condicoes.valor_centavos)),
        h("span", { class: "vg-quem" },
          h("span", { class: "vg-rota" }, rota(o.carga)),
          h("span", { class: "vg-embarcador" }, item.embarcador.nome ?? item.embarcador.cnpj)),
        // todos os motivos, o mais grave primeiro (em destaque)
        h("span", { class: "vg-motivos" }, (motivos.length ? motivos
          : [{ status: "atencao", texto: "Aguardando decisão" }])
          .map((m) => h("span", { class: `vg-status ${m.status}` }, m.texto))));
    });

  const embarcadores = v.embarcadores.length === 0
    ? h("p", { class: "vg-vazio" }, "Nenhum frete fechado ainda neste mês.")
    : v.embarcadores.map((e) => h("div", { class: "vg-linha vg-embarcadores" },
      h("span", { class: "vg-quem" }, h("span", { class: "vg-nome" }, e.nome ?? e.cnpj)),
      h("span", { class: "vg-valor" }, contar(e.fretes, "frete", "fretes")),
      h("span", { class: "vg-total" }, reais(e.total_centavos))));

  return h("section", { class: "visao-geral" },
    h("div", { class: "vg-mes" },
      `${api.sessao()?.razaoSocial ?? ""} · ${MESES[Number(mes) - 1]} de ${ano}`),
    h("h1", { tabindex: -1 }, "Visão geral"),
    h("div", { class: "vg-numeros" },
      numero("Fretes fechados no mês", reais(fechados.total_centavos),
        fechados.quantidade === 0 ? "Nenhum frete fechado ainda neste mês."
          : `${contar(fechados.quantidade, "frete", "fretes")}, `
            + (fechados.quantidade === 1 ? "com prova verificável" : "todos com prova verificável")),
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
