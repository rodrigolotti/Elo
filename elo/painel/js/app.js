// Painel da transportadora: telas e navegação (rotas por #hash, sem framework).

import * as api from "./api.js";
import { assinar, chavePublicaBase64, gerarParDeChaves, temEd25519 } from "./assinatura.js";
import { hashSha256 } from "./canonico.js";
import {
  mensagemChaveAntiga, telaRecuperacaoDoCadastro, telaRecuperar, telaSeguranca,
} from "./chave.js";
import { conferirRascunho } from "./conferencia.js";
import {
  apagarChave, guardarChave, listarChaves, pedirArmazenamentoPersistente,
} from "./cofre.js";
import { h, limpar } from "./dom.js";
import { telaVisaoGeral } from "./visao.js";
import {
  avisar, avisarErro, cabecalho, campo, confirmar, dados, executando, mono, vazio,
} from "./ui.js";
import {
  FATOR_CUBAGEM_KG_POR_M3, TIPOS_CARGA, UFS, centavosDeReais, chavePublicaValida, dataBr,
  datasEmBrasilia, emBrasilia,
  isoDeBrasilia, localDeBrasilia, metrosCubicos, numero, pesoCubadoKg, prazoAte, reais,
  resumoFrete, rota, soDigitos,
} from "./formatos.js";

const topo = document.getElementById("topo");
const conteudo = document.getElementById("conteudo");

function responsavelSalvo() {
  return localStorage.getItem("elo.responsavel") ?? "";
}

// ---------- topo e navegação ----------

const MENU = [
  ["/visao-geral", "Visão geral"],
  ["/aprovacoes", "Aprovações"],
  ["/agentes", "Agentes"],
  ["/mandatos", "Mandatos"],
  ["/historico", "Histórico"],
  ["/seguranca", "Segurança"],
];

// Enquanto um passo obrigatório está na tela (o arquivo de recuperação do
// cadastro), o menu some e qualquer saída pede confirmação.
let passoObrigatorio = null; // { hash, titulo, corpo }

function bloquearSaida(bloqueio) {
  passoObrigatorio = bloqueio;
}

function liberarSaida() {
  passoObrigatorio = null;
}

window.addEventListener("beforeunload", (evento) => {
  if (passoObrigatorio) {
    evento.preventDefault();
    evento.returnValue = ""; // o navegador mostra a pergunta dele
  }
});

// ---------- modo demonstração (só aparece se o servidor estiver com ELO_DEMO=ligado) ----------

let demo; // undefined: ainda não consultado; null: modo desligado

async function consultarDemo() {
  if (demo !== undefined) return demo;
  try {
    demo = await api.obter("/demo/agente");
  } catch {
    demo = null;
  }
  return demo;
}

function mandatoAtivo(item) {
  const agora = new Date();
  const l = item.mandato.limites;
  return !item.revogado_em && new Date(l.valido_de) <= agora && agora <= new Date(l.valido_ate);
}

/** Prepara (se preciso) e simula um pedido de cotação com o agente de demonstração. */
async function simularPedido(botao) {
  await executando(botao, async () => {
    try {
      const { cnpj } = api.sessao();
      let agente = (await api.obter("/agentes")).find((a) => a.chave_publica === demo.chave_publica);
      const preparado = agente && (await api.obter("/mandatos"))
        .some((m) => m.mandato.agente_id === agente.agente_id && mandatoAtivo(m));
      if (!preparado) {
        const l = demo.limites_padrao;
        const ok = await confirmar({
          titulo: "Preparar demonstração",
          corpo: ["Para simular pedidos, a transportadora autoriza o agente de demonstração, "
            + "que roda no servidor só neste modo.",
          `Você vai assinar o cadastro do ${demo.nome} e um mandato: ${l.ufs_origem.join(", ")} → `
            + `${l.ufs_destino.join(", ")}, carga geral, até ${reais(l.valor_max_centavos)} por `
            + "frete, por 30 dias."],
          botao: "Assinar e preparar",
        });
        if (!ok) return;
        if (!agente) {
          const corpo = { cnpj, nome: demo.nome, chave_id: demo.chave_id,
                          chave_publica: demo.chave_publica };
          agente = await api.assinarEEnviar({
            rotaRascunho: "/agentes/rascunho", corpo, rotaEnvio: "/agentes",
            esperado: { tipo: "certificado_agente", ...corpo },
          });
        }
        const agora = new Date();
        const limites = { ...l, valido_de: isoDeBrasilia(localDeBrasilia(agora)),
                          valido_ate: isoDeBrasilia(localDeBrasilia(new Date(agora.getTime() + 30 * 864e5))) };
        await api.assinarEEnviar({
          rotaRascunho: "/mandatos/rascunho",
          corpo: { cnpj, agente_id: agente.agente_id, limites }, rotaEnvio: "/mandatos",
          esperado: { tipo: "mandato", cnpj, agente_id: agente.agente_id, limites },
        });
      }
      const r = await api.enviar("/demo/simular", {});
      const aguardando = r.ofertas.filter((o) => o.status === "aguardando_aprovacao").length;
      avisar(`Pedido simulado de ${r.embarcador.nome}: ${r.ofertas.length} ofertas. `
        + `${r.ofertas.length - aguardando} foi direto ao embarcador e ${aguardando} aguardam `
        + "a sua decisão.");
      if (location.hash === "#/aprovacoes") recarregar();
      else location.hash = "#/aprovacoes";
    } catch (erro) {
      avisarErro(erro);
    }
  });
}

function desenharTopo(caminho, pendentes, semRecuperacao) {
  const atual = api.sessao();
  const emPasso = passoObrigatorio !== null;
  limpar(topo).append(h("div", { class: "topo-dentro" },
    h("a", { class: "marca", href: atual ? "#/visao-geral" : "#/entrar" },
      h("span", { class: "marca-elo" }, "Elo"),
      atual && !emPasso ? null : h("span", { class: "marca-sub" },
        emPasso ? "Cadastro: passo 2 de 2" : "Painel da transportadora")),
    atual && !emPasso ? h("nav", { "aria-label": "Seções" },
      MENU.map(([alvo, nome]) => h("a", {
        href: `#${alvo}`, class: alvo === caminho ? "ativo" : null,
        "aria-current": alvo === caminho ? "page" : null,
      }, nome,
      alvo === "/aprovacoes" && pendentes ? h("span", { class: "contador" }, pendentes) : null,
      alvo === "/seguranca" && semRecuperacao
        ? h("span", { class: "contador", title: "Sem arquivo de recuperação" }, "!") : null)))
      : null,
    atual && !emPasso && demo ? h("button", {
      type: "button", class: "botao secundario pequeno so-desktop",
      onclick: (e) => simularPedido(e.currentTarget),
    }, "Simular pedido de cotação") : null,
    atual && !emPasso ? h("div", { class: "quem" },
      h("span", { class: "quem-nome" }, atual.razaoSocial),
      h("span", { class: "quem-cnpj mono" }, atual.cnpj),
      h("button", { type: "button", class: "botao secundario pequeno", onclick: async () => {
        await api.sair().catch(() => {});
        location.hash = "#/entrar";
      } }, "Sair"))
      : null));
}

const ROTAS = {
  "/visao-geral": telaVisaoGeral,
  "/entrar": telaEntrar,
  "/cadastro": telaCadastro,
  "/aprovacoes": telaAprovacoes,
  "/agentes": telaAgentes,
  "/mandatos": telaMandatos,
  "/historico": telaHistorico,
  "/seguranca": telaSeguranca,
  "/recuperar": () => telaRecuperar(conteudo),
};
const ABERTAS = new Set(["/entrar", "/cadastro", "/recuperar"]);
let navegacao = 0;
let perguntandoSaida = false;

async function navegar() {
  if (passoObrigatorio) {
    if (location.hash === passoObrigatorio.hash || perguntandoSaida) return;
    perguntandoSaida = true;
    const sair = await confirmar({
      titulo: passoObrigatorio.titulo, corpo: passoObrigatorio.corpo,
      botao: "Sair mesmo assim", cancelar: "Continuar no passo", perigo: true,
    });
    perguntandoSaida = false;
    if (!sair) {
      location.hash = passoObrigatorio.hash; // volta sem refazer a tela
      return;
    }
    liberarSaida();
  }
  const minha = ++navegacao;
  const logado = api.sessao() !== null;
  const caminho = location.hash.slice(1) || (logado ? "/visao-geral" : "/entrar");
  if (logado && caminho === "/cadastro") {
    // já cadastrada: se faltar a recuperação (ex.: F5 no passo 2), retoma o passo
    try {
      const org = await api.obter("/organizacao");
      if (minha !== navegacao) return;
      if (!org.recuperacao) {
        passo2DoCadastro(await pedirArmazenamentoPersistente(), true);
        return;
      }
      location.hash = "#/visao-geral";
    } catch (erro) {
      avisarErro(erro);
    }
    return;
  }
  if (!ABERTAS.has(caminho) && !logado) {
    location.hash = "#/entrar";
    return;
  }
  desenharTopo(caminho);
  limpar(conteudo).append(h("p", { class: "carregando" }, "Carregando…"));
  try {
    const tela = ROTAS[caminho] ?? (async () => vazio("Página não encontrada."));
    const no = await tela();
    if (minha !== navegacao) return; // a pessoa já foi para outra tela
    limpar(conteudo).append(no);
    conteudo.querySelector("h1")?.focus();
    if (logado) {
      const [pendentes, org] = await Promise.all([
        api.obter("/ofertas?status=aguardando_aprovacao"), api.obter("/organizacao"),
        consultarDemo()]);
      if (minha !== navegacao) return;
      desenharTopo(caminho, pendentes.length, !org.recuperacao);
      if (!org.recuperacao && caminho !== "/seguranca") conteudo.prepend(faixaSemRecuperacao());
    }
  } catch (erro) {
    if (minha !== navegacao) return;
    limpar(conteudo).append(vazio("Não foi possível carregar esta tela."));
    avisarErro(erro);
  }
}

const recarregar = () => navegar();

// ---------- entrar ----------

async function telaEntrar() {
  if (!(await temEd25519())) {
    return h("section", {}, cabecalho("Navegador desatualizado"),
      h("div", { class: "caixa alerta" },
        h("p", {}, "Este navegador não tem o recurso de segurança que o painel usa para guardar "
          + "o acesso da transportadora só neste computador."),
        h("p", {}, "Use uma versão atual do Chrome, Edge (137 ou mais nova), Firefox (129 ou "
          + "mais nova) ou Safari (17 ou mais novo).")));
  }
  const registros = await listarChaves();
  const secao = h("section", {}, cabecalho("Entrar",
    "Entre com um clique. Seu acesso fica guardado neste computador, sem senha."));
  if (registros.length === 0) {
    secao.append(vazio("Nenhuma transportadora tem acesso guardado neste computador.",
      h("p", { class: "ajuda" }, "Se a transportadora foi cadastrada em outro computador, ou se "
        + "este navegador foi limpo, use o arquivo de recuperação e a frase-senha para voltar "
        + "a entrar."),
      h("div", { class: "acoes centro" },
        h("a", { class: "botao secundario", href: "#/recuperar" }, "Recuperar acesso com o arquivo"),
        h("a", { class: "botao primario", href: "#/cadastro" }, "Cadastrar transportadora"))));
    return secao;
  }
  secao.append(h("div", { class: "lista-cartoes" }, registros.map((r) => {
    const botao = h("button", { type: "button", class: "botao primario" }, "Entrar");
    const erro = h("div", { class: "erro-cartao" });
    botao.addEventListener("click", () => executando(botao, async () => {
      limpar(erro);
      try {
        await api.entrar(r);
        location.hash = "#/visao-geral";
      } catch (e) {
        erro.append(h("div", { class: "caixa erro", role: "alert" }, mensagemChaveAntiga(e)));
      }
    }));
    return h("article", { class: "cartao" },
      h("div", { class: "cartao-org" },
        h("div", {}, h("h2", {}, r.razaoSocial), mono(r.cnpj),
          h("p", { class: "ajuda" }, `Acesso guardado neste computador em ${emBrasilia(r.criadaEm)}`)),
        botao),
      erro);
  })), h("p", { class: "links" },
    h("a", { href: "#/recuperar" }, "Perdi o acesso neste computador ou estou em outro computador"),
    h("a", { href: "#/cadastro" }, "Cadastrar outra transportadora")));
  return secao;
}

// ---------- cadastro da organização ----------

/**
 * Passo 2 do cadastro (arquivo de recuperação): sem menu, e sair pede
 * confirmação. Também é retomado se a pessoa recarregar a página (F5) antes
 * de terminar: a transportadora logada e sem recuperação volta a este passo.
 */
async function passo2DoCadastro(persistente, retomado = false) {
  if (location.hash !== "#/cadastro") location.hash = "#/cadastro";
  bloquearSaida({
    hash: "#/cadastro",
    titulo: "Sair sem guardar o arquivo de recuperação?",
    corpo: ["A transportadora ainda não tem arquivo de recuperação. Sem ele, se você "
      + "trocar de computador ou o navegador for limpo, perde o acesso à conta da "
      + "transportadora.",
    "Dá para criar depois, na aba Segurança, mas o melhor é terminar agora."],
  });
  desenharTopo("/cadastro", 0, true);
  const registrado = await telaRecuperacaoDoCadastro(conteudo, { persistente, retomado });
  if (passoObrigatorio === null) return; // a pessoa saiu antes de terminar
  liberarSaida();
  location.hash = registrado ? "#/agentes" : "#/seguranca";
}

/** Faixa em todas as telas enquanto a transportadora não tiver recuperação. */
function faixaSemRecuperacao() {
  return h("div", { class: "caixa alerta faixa-recuperacao", role: "alert" },
    h("p", {}, h("strong", {}, "A transportadora ainda não tem arquivo de recuperação."),
      " Se você trocar de computador ou o navegador for limpo, perde o acesso à conta."),
    h("a", { class: "botao primario pequeno", href: "#/seguranca" }, "Criar agora"));
}

function avisoPerdaDeChave() {
  return h("div", { class: "caixa alerta" },
    h("h2", {}, "Importante: o acesso da transportadora fica guardado neste computador"),
    h("p", {}, "Ao cadastrar, o acesso é criado e guardado só neste navegador. Ninguém, nem a "
      + "equipe do Elo, consegue copiá-lo ou usá-lo por você."),
    h("p", {}, "Ele se perde se o navegador for limpo ou desinstalado, se você trocar de "
      + "computador, usar janela anônima ou, no Safari, ficar 7 dias sem abrir o painel."),
    h("p", {}, h("strong", {}, "A única forma de voltar a entrar é o arquivo de recuperação "
      + "com a frase-senha,"), " que você cria no próximo passo. Sem eles, a transportadora não "
      + "consegue mais cadastrar agentes, dar mandatos, aprovar ou recusar ofertas, nem ",
      h("strong", {}, "revogar um mandato"),
      ". Os acordos já fechados continuam valendo."),
    h("ul", {},
      h("li", {}, "Use um computador da empresa e um navegador só para o painel."),
      h("li", {}, "Não use janela anônima e não limpe os dados deste site."),
      h("li", {}, "Guarde o arquivo de recuperação e a frase-senha em lugares separados.")));
}

async function telaCadastro() {
  if (!(await temEd25519())) return telaEntrar();
  const cnpj = h("input", { name: "cnpj", required: true, inputmode: "numeric",
                            autocomplete: "off", placeholder: "00.000.000/0000-00" });
  const razao = h("input", { name: "razao_social", required: true, maxlength: 200,
                             autocomplete: "organization" });
  const entendi = h("input", { type: "checkbox", required: true });
  const botao = h("button", { type: "submit", class: "botao primario" },
    "Cadastrar transportadora");

  const form = h("form", { class: "formulario" },
    campo("CNPJ", cnpj),
    campo("Razão social", razao),
    avisoPerdaDeChave(),
    h("label", { class: "confirmacao" }, entendi,
      h("span", {}, "Entendi: sem o arquivo de recuperação e a frase-senha, perder o acesso "
        + "deste computador não tem volta.")),
    h("div", { class: "acoes" }, h("a", { class: "botao secundario", href: "#/entrar" }, "Voltar"),
      botao));

  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    executando(botao, async () => {
      let cadastrado = null;
      try {
        const par = await gerarParDeChaves();
        const chavePublica = await chavePublicaBase64(par.publicKey);
        const chaveId = `org-${soDigitos(cnpj.value)}#${new Date().toISOString().slice(0, 10)}`;
        const razaoSocial = razao.value.trim();
        // o rascunho valida CNPJ e duplicidade antes de guardar qualquer coisa
        const { rascunho } = await api.enviar("/organizacoes/rascunho", {
          cnpj: cnpj.value, razao_social: razaoSocial, chave_id: chaveId,
          chave_publica: chavePublica,
        });
        conferirRascunho(rascunho, { tipo: "organizacao", razao_social: razaoSocial,
                                     chave_id: chaveId, chave_publica: chavePublica });
        if (soDigitos(rascunho.cnpj) !== soDigitos(cnpj.value)) {
          throw new Error("O Elo devolveu um CNPJ diferente do informado; nada foi registrado.");
        }
        const registro = { cnpj: rascunho.cnpj, razaoSocial, chaveId, par, chavePublica,
                           privada: par.privateKey };
        await guardarChave(registro).catch(() => {
          throw new Error("Esta transportadora já tem acesso guardado neste computador.");
        });
        cadastrado = rascunho.cnpj;
        await api.enviar("/organizacoes", await assinar(rascunho, par.privateKey, chaveId));
        cadastrado = null; // deu certo: a chave fica
        const persistente = await pedirArmazenamentoPersistente();
        await api.entrar(registro);
        await passo2DoCadastro(persistente);
      } catch (erro) {
        if (cadastrado) await apagarChave(cadastrado).catch(() => {});
        avisarErro(erro);
      }
    });
  });

  return h("section", {}, cabecalho("Cadastrar organização",
    "Cadastre a transportadora no Elo. O acesso é criado aqui, neste computador."),
    form);
}

// ---------- fila de aprovação ----------

function campoResponsavel() {
  const entrada = h("input", { required: true, maxlength: 200, value: responsavelSalvo(),
                               autocomplete: "name" });
  entrada.addEventListener("change", () =>
    localStorage.setItem("elo.responsavel", entrada.value.trim()));
  return { entrada, rotulo: campo("Seu nome (fica registrado na decisão)", entrada) };
}

/** "Comercial Exemplo Ltda" + CNPJ + quantos fretes já foram fechados com ele. */
function blocoEmbarcador(embarcador) {
  const n = embarcador.fretes_fechados;
  const historico = n === 0 ? "Primeiro frete com este embarcador"
    : `${n} frete${n > 1 ? "s" : ""} já fechado${n > 1 ? "s" : ""} com ele`;
  return h("div", {},
    h("div", {}, embarcador.nome ?? "Nome não informado no pedido"),
    h("div", { class: "ajuda" }, mono(embarcador.cnpj), ` · ${historico}`));
}

/** "carga geral · 3.200 kg · 14,5 m³" e, embaixo, o peso cubado. */
/** Nome da categoria; texto antigo fora da lista aparece com o aviso. */
function nomeDoTipo(descricao) {
  return descricao.na_lista ? descricao.nome
    : `${descricao.nome} (texto antigo, fora da lista da ANTT)`;
}

function blocoCarga(carga, tipoCarga) {
  const cubado = pesoCubadoKg(carga.volume_litros);
  return h("div", {},
    h("div", {}, `${nomeDoTipo(tipoCarga)} · ${numero(carga.peso_kg)} kg · `
      + metrosCubicos(carga.volume_litros)),
    h("div", { class: "ajuda" }, `Peso cubado: ${numero(cubado)} kg `
      + `(${FATOR_CUBAGEM_KG_POR_M3} kg/m³)`
      + (cubado > carga.peso_kg ? ", maior que o peso real" : "")));
}

const AVISO_PISO = "abaixo do piso mínimo da ANTT";
const PISO_NAO_CONFERIDO = "não foi possível conferir o piso da ANTT";
const MINIMO_JUSTIFICATIVA = 10;
const maiuscula = (texto) => texto.charAt(0).toUpperCase() + texto.slice(1);

/**
 * Uma caixa só: os motivos da aprovação (os do piso já com os números do frete)
 * e, embaixo, os dados do frete e do piso da ANTT, sem repetir a frase do piso.
 */
function caixaAprovacao(violacoes, frete) {
  const d = frete.distancia;
  const p = frete.piso;
  const estimada = p?.estimado;
  const apoioEstimado = p && estimada
    ? ` Pela distância estimada, ficaria ${reais(Math.abs(p.folga_centavos))} `
      + `${p.folga_centavos < 0 ? "abaixo" : "acima"} do piso.` : "";
  const motivos = violacoes.map((v) => {
    if (v.startsWith(AVISO_PISO) && p && !estimada) {
      return h("li", { class: "forte" }, `${reais(-p.folga_centavos)} abaixo do piso mínimo da `
        + "ANTT: risco de multa e indenização.");
    }
    if (v.includes(PISO_NAO_CONFERIDO)) {
      return h("li", { class: "forte" }, `${maiuscula(v)}.`
        + (v.startsWith("distância") ? apoioEstimado : ""));
    }
    return h("li", {}, maiuscula(datasEmBrasilia(v)));
  });
  const pisoEhMotivo = violacoes.some((v) => v.startsWith(AVISO_PISO) || v.includes(PISO_NAO_CONFERIDO));
  let situacao = null;
  if (p && !pisoEhMotivo) {
    situacao = p.folga_centavos >= 0
      ? h("p", { class: "piso-ok" }, `Acima do piso da ANTT em ${reais(p.folga_centavos)}`
        + `${estimada ? " (pela distância estimada)" : ""}.`)
      : h("p", { class: "piso-alerta" }, `Pela distância estimada, ficaria `
        + `${reais(-p.folga_centavos)} abaixo do piso da ANTT. Confirme a distância com o agente.`);
  }
  const modalidade = frete.modalidade === "lotacao" ? "Lotação"
    : frete.modalidade === "fracionada" ? "Fracionada" : "Não informada pelo agente";
  const distancia = !d.km ? `Não disponível (${d.detalhe})`
    : d.origem === "agente" ? `${numero(d.km)} km (informada pelo agente)`
    : `≈ ${numero(d.km)} km (estimada: ${d.detalhe})`;
  const data = p ? p.vigencia_desde.split("-").reverse().join("/") : "";
  return h("div", { class: "caixa motivo" },
    h("h3", {}, "Por que precisa de aprovação"),
    h("ul", {}, motivos),
    situacao,
    h("h4", {}, "Frete e piso da ANTT"),
    dados([
      ["Modalidade", `${modalidade}${frete.eixos ? ` · ${frete.eixos} eixos` : ""}`],
      ["Distância", distancia],
      frete.valor_por_km_centavos ? ["Valor por km", `${reais(frete.valor_por_km_centavos)}/km`
        + (d.origem === "estimada" ? " (pela distância estimada)" : "")] : null,
      ["Piso ANTT", p ? `${reais(p.piso_centavos)}${estimada ? " (estimado)" : ""} · ${p.nome}, `
        + `${p.eixos} eixos · tabela vigente desde ${data}` : frete.sem_piso],
    ]));
}

function cartaoOferta(item, agentes) {
  const { oferta, violacoes, embarcador } = item;
  const c = oferta.condicoes;
  const vencida = new Date(oferta.valida_ate) < new Date();
  const frete = resumoFrete(oferta);
  const nomeEmbarcador = embarcador.nome ?? embarcador.cnpj;
  const aprovar = h("button", { type: "button", class: "botao primario" }, "Aprovar");
  const recusar = h("button", { type: "button", class: "botao perigo-contorno" }, "Recusar");

  const abaixoDoPiso = violacoes.some((v) => v.startsWith(AVISO_PISO));

  async function decidir(botao, tipo) {
    const responsavel = campoResponsavel();
    const motivo = h("textarea", { maxlength: 1000, rows: 3 });
    const justificativa = h("textarea", { maxlength: 1000, rows: 3, name: "justificativa",
                                          required: true, minlength: MINIMO_JUSTIFICATIVA });
    const resumo = `${frete.charAt(0).toLowerCase()}${frete.slice(1)} para ${nomeEmbarcador}`;
    const ok = await confirmar(tipo === "aprovacao" ? {
      titulo: "Aprovar oferta",
      corpo: [`Você vai aprovar o ${resumo}. A aprovação fica registrada em nome da `
              + "transportadora e não pode ser apagada.",
              abaixoDoPiso ? h("p", { class: "aviso-forte" }, "Este frete está abaixo do piso "
                + "mínimo da ANTT: risco de multa e indenização. Para aprovar, escreva a "
                + "justificativa; ela fica registrada e assinada junto com a aprovação.") : null,
              "Depois disso o embarcador pode aceitá-la pelo link enviado pelo agente."]
        .filter(Boolean),
      botao: "Aprovar oferta",
      campos: [responsavel.rotulo,
        abaixoDoPiso ? campo("Justificativa (obrigatória: frete abaixo do piso da ANTT)",
          justificativa, `Pelo menos ${MINIMO_JUSTIFICATIVA} caracteres.`) : null].filter(Boolean),
    } : {
      titulo: "Recusar oferta",
      corpo: [`Você vai recusar o ${resumo}. A recusa é definitiva: a oferta não `
              + "poderá mais ser aprovada nem aceita."],
      botao: "Recusar oferta",
      perigo: true,
      campos: [responsavel.rotulo, campo("Motivo (opcional)", motivo)],
    });
    if (!ok) return;
    const nome = responsavel.entrada.value.trim();
    if (!nome) return avisar("Informe seu nome para registrar a decisão.", "erro");
    const textoJustificativa = justificativa.value.trim();
    if (tipo === "aprovacao" && abaixoDoPiso && textoJustificativa.length < MINIMO_JUSTIFICATIVA) {
      return avisar(`Para aprovar frete abaixo do piso da ANTT, escreva a justificativa `
        + `(pelo menos ${MINIMO_JUSTIFICATIVA} caracteres).`, "erro");
    }
    localStorage.setItem("elo.responsavel", nome);
    await executando(botao, async () => {
      try {
        const oferta_hash = await hashSha256(oferta);
        const base = `/ofertas/${encodeURIComponent(oferta.id)}`;
        if (tipo === "aprovacao") {
          await api.assinarEEnviar({
            rotaRascunho: `${base}/aprovacao/rascunho`,
            corpo: { aprovado_por: nome, ...(abaixoDoPiso ? { justificativa: textoJustificativa } : {}) },
            rotaEnvio: `${base}/aprovacao`,
            esperado: { tipo: "aprovacao", oferta_hash, aprovado_por: nome,
                        ...(abaixoDoPiso ? { justificativa: textoJustificativa } : {}) },
          });
          avisar(`${frete} aprovado e enviado ao embarcador.`);
        } else {
          const texto = motivo.value.trim();
          await api.assinarEEnviar({
            rotaRascunho: `${base}/recusa/rascunho`, corpo: { recusado_por: nome, motivo: texto },
            rotaEnvio: `${base}/recusa`,
            esperado: { tipo: "recusa", oferta_hash, recusado_por: nome, motivo: texto },
          });
          avisar(`${frete} recusado. O embarcador não poderá aceitá-lo.`);
        }
        recarregar();
      } catch (erro) {
        avisarErro(erro);
      }
    });
  }
  aprovar.addEventListener("click", () => decidir(aprovar, "aprovacao"));
  recusar.addEventListener("click", () => decidir(recusar, "recusa"));

  const agente = agentes.get(oferta.emissor.agente_id);
  return h("article", { class: "cartao" },
    h("div", { class: "cartao-topo" },
      h("div", {}, h("h2", {}, `${reais(c.valor_centavos)} · ${rota(oferta.carga)}`),
        h("div", { class: "ajuda" }, nomeEmbarcador, " · ", mono(oferta.id))),
      h("span", { class: `etiqueta ${vencida ? "ruim" : "neutro"}`,
                  title: `Válida até ${emBrasilia(oferta.valida_ate)}` },
        prazoAte(oferta.valida_ate))),
    caixaAprovacao(violacoes, item.frete),
    dados([
      ["Embarcador", blocoEmbarcador(embarcador)],
      ["Rota", rota(oferta.carga)],
      ["Carga", blocoCarga(oferta.carga, item.tipo_carga)],
      ["Coleta até", dataBr(c.coleta_ate)],
      ["Prazo de entrega", `${c.prazo_entrega_dias_uteis} dias úteis`],
      c.inclui.length ? ["Inclui", c.inclui.join(", ")] : null,
      ["Válida até", `${emBrasilia(oferta.valida_ate)} (${prazoAte(oferta.valida_ate)})`],
      ["Emitida em", emBrasilia(oferta.emitida_em)],
      ["Agente", agente ? agente.nome : oferta.emissor.agente_id],
      ["Mandato", mono(oferta.mandato_id)],
    ]),
    h("div", { class: "acoes" }, recusar, aprovar));
}

async function telaAprovacoes() {
  const [pendentes, agentes] = await Promise.all([
    api.obter("/ofertas?status=aguardando_aprovacao"), api.obter("/agentes")]);
  const porId = new Map(agentes.map((a) => [a.agente_id, a]));
  return h("section", {}, cabecalho("Ofertas aguardando aprovação",
    "Ofertas que o agente fez fora do mandato. Nada sai sem a sua aprovação."),
    pendentes.length === 0
      ? vazio("Nenhuma oferta aguardando aprovação.")
      : h("div", { class: "lista-cartoes" },
        pendentes.sort((a, b) => a.oferta.emitida_em.localeCompare(b.oferta.emitida_em))
          .map((item) => cartaoOferta(item, porId))));
}

// ---------- agentes ----------

async function telaAgentes() {
  const atual = api.sessao();
  const agentes = await api.obter("/agentes");
  const nome = h("input", { required: true, maxlength: 200, placeholder: "Agente de cotação" });
  const chaveId = h("input", { required: true, maxlength: 100, class: "mono",
                               placeholder: "agente-cotacao#k1" });
  const chavePublica = h("textarea", { required: true, rows: 2, class: "mono",
                                       placeholder: "Fornecida por quem instalou o agente" });
  const botao = h("button", { type: "submit", class: "botao primario" }, "Cadastrar agente");
  const form = h("form", { class: "formulario cartao" },
    h("h2", {}, "Novo agente"),
    h("p", { class: "ajuda" }, "O agente é o sistema que faz as cotações. Quem o instalou "
      + "fornece o identificador e a chave pública dele; cole aqui. A transportadora registra "
      + "que esse agente fala em nome dela."),
    campo("Nome", nome),
    campo("Identificador da chave", chaveId),
    campo("Chave pública do agente", chavePublica),
    h("div", { class: "acoes" }, botao));
  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    const pub = chavePublica.value.trim();
    if (!chavePublicaValida(pub)) {
      avisar("A chave pública do agente não parece válida. Confira com quem instalou o agente "
        + "e cole de novo, sem cortar nenhum caractere.", "erro");
      return;
    }
    executando(botao, async () => {
      try {
        const corpo = { cnpj: atual.cnpj, nome: nome.value.trim(),
                        chave_id: chaveId.value.trim(), chave_publica: pub };
        const agente = await api.assinarEEnviar({
          rotaRascunho: "/agentes/rascunho", corpo, rotaEnvio: "/agentes",
          esperado: { tipo: "certificado_agente", cnpj: corpo.cnpj, nome: corpo.nome,
                      chave_id: corpo.chave_id, chave_publica: pub },
        });
        avisar(`Agente “${agente.nome}” cadastrado.`);
        recarregar();
      } catch (erro) {
        avisarErro(erro);
      }
    });
  });

  const tabela = agentes.length === 0 ? vazio("Nenhum agente ainda.") : h("table", {},
    h("thead", {}, h("tr", {}, ["Nome", "ID", "Chave", "Emitido em"].map((t) => h("th", {}, t)))),
    h("tbody", {}, agentes.map((a) => h("tr", {},
      h("td", {}, a.nome), h("td", {}, mono(a.agente_id)), h("td", {}, mono(a.chave_id)),
      h("td", {}, emBrasilia(a.emitido_em))))));
  return h("section", {}, cabecalho("Agentes",
    "Os sistemas que fazem cotações em nome da transportadora, cada um dentro do mandato que você der."),
    h("div", { class: "duas-colunas" }, h("div", {}, tabela), form));
}

// ---------- mandatos ----------

function situacaoMandato(item) {
  const agora = new Date();
  const { limites } = item.mandato;
  if (item.revogado_em) return ["Revogado", "ruim"];
  if (new Date(limites.valido_ate) < agora) return ["Vencido", "neutro"];
  if (new Date(limites.valido_de) > agora) return ["Ainda não vigente", "neutro"];
  return ["Ativo", "bom"];
}

function cartaoMandato(item) {
  const { mandato } = item;
  const l = mandato.limites;
  const [situacao, classe] = situacaoMandato(item);
  const revogar = item.revogado_em ? null
    : h("button", { type: "button", class: "botao perigo-contorno" }, "Revogar mandato");
  revogar?.addEventListener("click", async () => {
    const ok = await confirmar({
      titulo: "Revogar mandato",
      corpo: [`Você vai revogar o mandato de ${item.agente_nome ?? mandato.agente_id} `
              + `(até ${reais(l.valor_max_centavos)} por frete).`,
              "É irreversível. A partir de agora, ofertas feitas com este mandato vão para "
              + "aprovação humana. Compromissos já firmados continuam válidos."],
      botao: "Confirmar revogação",
      perigo: true,
    });
    if (!ok) return;
    await executando(revogar, async () => {
      try {
        const base = `/mandatos/${encodeURIComponent(mandato.id)}/revogacao`;
        await api.assinarEEnviar({
          rotaRascunho: `${base}/rascunho`, corpo: {}, rotaEnvio: base,
          esperado: { tipo: "revogacao", mandato_id: mandato.id },
        });
        avisar(`Mandato de ${item.agente_nome ?? mandato.agente_id} revogado. Novas ofertas `
          + "dele vão para a sua aprovação.");
        recarregar();
      } catch (erro) {
        avisarErro(erro);
      }
    });
  });
  return h("article", { class: "cartao" },
    h("div", { class: "cartao-topo" },
      h("div", {}, h("h2", {}, item.agente_nome ?? mandato.agente_id), mono(mandato.id)),
      h("span", { class: `etiqueta ${classe}` }, situacao)),
    dados([
      ["Valor máximo", reais(l.valor_max_centavos)],
      ["UFs de origem", l.ufs_origem.join(", ")],
      ["UFs de destino", l.ufs_destino.join(", ")],
      ["Tipos de carga", item.tipos_carga.map(nomeDoTipo).join(", ")],
      ["Vale de", emBrasilia(l.valido_de)],
      ["Vale até", emBrasilia(l.valido_ate)],
      item.revogado_em ? ["Revogado em", emBrasilia(item.revogado_em)] : null,
    ]),
    revogar ? h("div", { class: "acoes" }, revogar) : null);
}

function seletorUfs(nome) {
  return h("fieldset", { class: "ufs" }, h("legend", {}, nome),
    UFS.map((uf) => h("label", {}, h("input", { type: "checkbox", value: uf }), uf)));
}

const ufsMarcadas = (fieldset) =>
  [...fieldset.querySelectorAll("input:checked")].map((i) => i.value);

async function telaMandatos() {
  const atual = api.sessao();
  const [mandatos, agentes] = await Promise.all([api.obter("/mandatos"), api.obter("/agentes")]);

  const agente = h("select", { required: true },
    h("option", { value: "" }, "Escolha o agente"),
    agentes.map((a) => h("option", { value: a.agente_id }, `${a.nome} (${a.agente_id})`)));
  const valor = h("input", { required: true, inputmode: "decimal", placeholder: "6.000,00" });
  const origem = seletorUfs("UFs de origem");
  const destino = seletorUfs("UFs de destino");
  const tipos = h("fieldset", { class: "tipos" }, h("legend", {}, "Tipos de carga (ANTT)"),
    Object.entries(TIPOS_CARGA).map(([valor, nome]) =>
      h("label", {}, h("input", { type: "checkbox", value: valor }), nome)));
  const agora = new Date();
  const de = h("input", { type: "datetime-local", required: true, value: localDeBrasilia(agora) });
  const ate = h("input", { type: "datetime-local", required: true,
                           value: localDeBrasilia(new Date(agora.getTime() + 30 * 864e5)) });
  const botao = h("button", { type: "submit", class: "botao primario" }, "Emitir mandato");

  const form = h("form", { class: "formulario cartao" },
    h("h2", {}, "Novo mandato"),
    h("p", { class: "ajuda" }, "O que o agente pode oferecer sozinho. Fora disso, a oferta "
      + "vem para a sua aprovação."),
    campo("Agente", agente),
    campo("Valor máximo por oferta (R$)", valor),
    origem, destino,
    tipos,
    campo("Vale de (horário de Brasília)", de),
    campo("Vale até (horário de Brasília)", ate),
    h("div", { class: "acoes" }, botao));

  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    executando(botao, async () => {
      try {
        const limites = {
          valor_max_centavos: centavosDeReais(valor.value),
          ufs_origem: ufsMarcadas(origem),
          ufs_destino: ufsMarcadas(destino),
          tipos_carga: [...tipos.querySelectorAll("input:checked")].map((i) => i.value),
          valido_de: isoDeBrasilia(de.value),
          valido_ate: isoDeBrasilia(ate.value),
        };
        if (!limites.ufs_origem.length || !limites.ufs_destino.length) {
          throw new Error("Marque ao menos uma UF de origem e uma de destino.");
        }
        if (!limites.tipos_carga.length) throw new Error("Marque ao menos um tipo de carga.");
        const mandato = await api.assinarEEnviar({
          rotaRascunho: "/mandatos/rascunho",
          corpo: { cnpj: atual.cnpj, agente_id: agente.value, limites },
          rotaEnvio: "/mandatos",
          esperado: { tipo: "mandato", cnpj: atual.cnpj, agente_id: agente.value, limites },
        });
        const nomeAgente = agentes.find((a) => a.agente_id === mandato.agente_id)?.nome
          ?? mandato.agente_id;
        avisar(`Mandato de ${nomeAgente} emitido: até ${reais(limites.valor_max_centavos)} `
          + "por frete.");
        recarregar();
      } catch (erro) {
        avisarErro(erro);
      }
    });
  });

  const ordenados = mandatos.sort((a, b) =>
    b.mandato.emitido_em.localeCompare(a.mandato.emitido_em));
  return h("section", {}, cabecalho("Mandatos",
    "O que cada agente pode oferecer sozinho. Revogue se um agente sair do controle."),
    h("div", { class: "duas-colunas" },
      h("div", { class: "lista-cartoes" },
        ordenados.length ? ordenados.map(cartaoMandato) : vazio("Nenhum mandato ainda.")),
      agentes.length ? form : vazio("Crie um agente antes de emitir um mandato.",
        h("a", { class: "botao primario", href: "#/agentes" }, "Ir para agentes"))));
}

// ---------- histórico ----------

async function telaHistorico() {
  const compromissos = await api.obter("/compromissos");
  compromissos.sort((a, b) => b.compromisso.aceite.em.localeCompare(a.compromisso.aceite.em));
  return h("section", {}, cabecalho("Histórico de compromissos",
    "Ofertas aceitas pelos embarcadores. Cada uma pode ser verificada por qualquer pessoa."),
    compromissos.length === 0 ? vazio("Nenhum compromisso firmado ainda.") : h("table", {},
      h("thead", {}, h("tr", {},
        ["Aceite em", "Embarcador", "Valor", "Rota", "Compromisso", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, compromissos.map(({ compromisso: cp, oferta, verificacao, embarcador }) => h("tr", {},
        h("td", {}, emBrasilia(cp.aceite.em)),
        h("td", {}, embarcador?.nome ?? mono(cp.aceite.cnpj),
          h("div", { class: "ajuda" }, embarcador?.nome ? mono(cp.aceite.cnpj) : null,
            embarcador?.nome ? " · " : null, `aceito por ${cp.aceite.nome}`)),
        h("td", { class: "numero" }, reais(oferta.condicoes.valor_centavos)),
        h("td", {}, rota(oferta.carga)),
        h("td", {}, mono(cp.id)),
        h("td", {}, h("a", { href: verificacao, target: "_blank", rel: "noopener" },
          "Verificar ↗")))))));
}

// ---------- início ----------

window.addEventListener("elo:sessao-expirada", () => {
  avisar("Sua sessão terminou. Entre de novo.", "alerta");
  location.hash = "#/entrar";
});
window.addEventListener("hashchange", navegar);
navegar();
