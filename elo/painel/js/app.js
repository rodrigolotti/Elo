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
import {
  avisar, avisarErro, cabecalho, campo, confirmar, dados, executando, mono, vazio,
} from "./ui.js";
import {
  UFS, centavosDeReais, chavePublicaValida, dataBr, emBrasilia, isoDeBrasilia, lista,
  localDeBrasilia, reais, rota, soDigitos,
} from "./formatos.js";

const topo = document.getElementById("topo");
const conteudo = document.getElementById("conteudo");

function responsavelSalvo() {
  return localStorage.getItem("elo.responsavel") ?? "";
}

// ---------- topo e navegação ----------

const MENU = [
  ["/aprovacoes", "Aprovações"],
  ["/agentes", "Agentes"],
  ["/mandatos", "Mandatos"],
  ["/historico", "Histórico"],
  ["/seguranca", "Segurança"],
];

function desenharTopo(caminho, pendentes, semRecuperacao) {
  const atual = api.sessao();
  limpar(topo).append(h("div", { class: "topo-dentro" },
    h("a", { class: "marca", href: atual ? "#/aprovacoes" : "#/entrar" },
      h("span", { class: "marca-elo" }, "Elo"),
      atual ? null : h("span", { class: "marca-sub" }, "Painel da transportadora")),
    atual ? h("nav", { "aria-label": "Seções" },
      MENU.map(([alvo, nome]) => h("a", {
        href: `#${alvo}`, class: alvo === caminho ? "ativo" : null,
        "aria-current": alvo === caminho ? "page" : null,
      }, nome,
      alvo === "/aprovacoes" && pendentes ? h("span", { class: "contador" }, pendentes) : null,
      alvo === "/seguranca" && semRecuperacao
        ? h("span", { class: "contador", title: "Sem chave de recuperação" }, "!") : null)))
      : null,
    atual ? h("div", { class: "quem" },
      h("span", { class: "quem-nome" }, atual.razaoSocial),
      h("span", { class: "quem-cnpj mono" }, atual.cnpj),
      h("button", { type: "button", class: "botao secundario pequeno", onclick: async () => {
        await api.sair().catch(() => {});
        location.hash = "#/entrar";
      } }, "Sair"))
      : null));
}

const ROTAS = {
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

async function navegar() {
  const minha = ++navegacao;
  const logado = api.sessao() !== null;
  const caminho = location.hash.slice(1) || (logado ? "/aprovacoes" : "/entrar");
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
        api.obter("/ofertas?status=aguardando_aprovacao"), api.obter("/organizacao")]);
      if (minha === navegacao) desenharTopo(caminho, pendentes.length, !org.recuperacao);
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
    return h("section", {}, cabecalho("Navegador sem suporte"),
      h("div", { class: "caixa alerta" },
        h("p", {}, "Este navegador não tem assinatura Ed25519 na Web Crypto API, "
          + "que o painel usa para que a chave da organização nunca saia do seu computador."),
        h("p", {}, "Use uma versão atual do Chrome, Edge (137 ou mais nova), Firefox (129 ou "
          + "mais nova) ou Safari (17 ou mais novo).")));
  }
  const registros = await listarChaves();
  const secao = h("section", {}, cabecalho("Entrar",
    "Escolha a organização. A entrada é feita assinando um desafio com a chave "
    + "guardada neste navegador; não há senha."));
  if (registros.length === 0) {
    secao.append(vazio("Nenhuma organização tem chave neste navegador.",
      h("p", { class: "ajuda" }, "Se a organização foi cadastrada em outro navegador, ou se os "
        + "dados deste navegador foram apagados, a chave não está aqui. Use o arquivo de "
        + "recuperação e a frase-senha para trocar a chave."),
      h("div", { class: "acoes centro" },
        h("a", { class: "botao secundario", href: "#/recuperar" }, "Recuperar com o arquivo"),
        h("a", { class: "botao primario", href: "#/cadastro" }, "Cadastrar organização"))));
    return secao;
  }
  secao.append(h("div", { class: "lista-cartoes" }, registros.map((r) => {
    const botao = h("button", { type: "button", class: "botao primario" }, "Entrar");
    const erro = h("div", { class: "erro-cartao" });
    botao.addEventListener("click", () => executando(botao, async () => {
      limpar(erro);
      try {
        await api.entrar(r);
        location.hash = "#/aprovacoes";
      } catch (e) {
        erro.append(h("div", { class: "caixa erro", role: "alert" }, mensagemChaveAntiga(e)));
      }
    }));
    return h("article", { class: "cartao" },
      h("div", { class: "cartao-org" },
        h("div", {}, h("h2", {}, r.razaoSocial), mono(r.cnpj),
          h("p", { class: "ajuda" }, `Chave ${r.chaveId}, criada em ${emBrasilia(r.criadaEm)}`)),
        botao),
      erro);
  })), h("p", { class: "links" },
    h("a", { href: "#/recuperar" }, "Perdi a chave deste navegador ou estou em outro computador"),
    h("a", { href: "#/cadastro" }, "Cadastrar outra organização")));
  return secao;
}

// ---------- cadastro da organização ----------

function avisoPerdaDeChave() {
  return h("div", { class: "caixa alerta" },
    h("h2", {}, "Atenção: a chave da organização fica só neste navegador"),
    h("p", {}, "Ao cadastrar, o navegador cria a chave privada da organização. Ela é marcada "
      + "como não exportável e nunca é enviada ao servidor: nem o Elo, nem este painel "
      + "conseguem copiá-la."),
    h("p", {}, "Ela se perde se você limpar os dados de navegação ou do site, desinstalar o "
      + "navegador, trocar de computador, usar janela anônima, ou, no Safari, ficar 7 dias "
      + "sem abrir o painel."),
    h("p", {}, h("strong", {}, "A única forma de recuperá-la é o arquivo de recuperação com a "
      + "frase-senha,"), " que você cria no próximo passo. Sem eles, a organização não consegue "
      + "mais criar agentes, emitir mandatos, aprovar ou recusar ofertas, nem ",
      h("strong", {}, "revogar um mandato"),
      ". Os compromissos já firmados continuam válidos e verificáveis."),
    h("ul", {},
      h("li", {}, "Use um perfil de navegador só para o painel, em um computador da empresa."),
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
    "Gerar chave e cadastrar");

  const form = h("form", { class: "formulario" },
    campo("CNPJ", cnpj),
    campo("Razão social", razao),
    avisoPerdaDeChave(),
    h("label", { class: "confirmacao" }, entendi,
      h("span", {}, "Entendi que, sem o arquivo de recuperação e a frase-senha, perder a chave "
        + "deste navegador é irreversível.")),
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
          throw new Error("o servidor devolveu outro CNPJ; nada foi assinado");
        }
        const registro = { cnpj: rascunho.cnpj, razaoSocial, chaveId, par, chavePublica,
                           privada: par.privateKey };
        await guardarChave(registro).catch(() => {
          throw new Error("Já existe uma chave desta organização neste navegador.");
        });
        cadastrado = rascunho.cnpj;
        await api.enviar("/organizacoes", await assinar(rascunho, par.privateKey, chaveId));
        cadastrado = null; // deu certo: a chave fica
        const persistente = await pedirArmazenamentoPersistente();
        await api.entrar(registro);
        avisar("Organização cadastrada. A chave está guardada neste navegador.");
        if (!persistente) {
          avisar("O navegador não garantiu armazenamento persistente: ele pode apagar a "
            + "chave se faltar espaço. Evite limpar os dados deste site.", "alerta");
        }
        desenharTopo("/cadastro", 0, true);
        telaRecuperacaoDoCadastro(conteudo);
      } catch (erro) {
        if (cadastrado) await apagarChave(cadastrado).catch(() => {});
        avisarErro(erro);
      }
    });
  });

  return h("section", {}, cabecalho("Cadastrar organização",
    "Cadastre a transportadora no Elo. A chave da organização é criada aqui, no navegador."),
    form);
}

// ---------- fila de aprovação ----------

function campoResponsavel() {
  const entrada = h("input", { required: true, maxlength: 200, value: responsavelSalvo(),
                               autocomplete: "name" });
  entrada.addEventListener("change", () =>
    localStorage.setItem("elo.responsavel", entrada.value.trim()));
  return { entrada, rotulo: campo("Seu nome (vai na assinatura)", entrada) };
}

function cartaoOferta(item, agentes) {
  const { oferta, violacoes } = item;
  const c = oferta.condicoes;
  const vencida = new Date(oferta.valida_ate) < new Date();
  const aprovar = h("button", { type: "button", class: "botao primario" }, "Aprovar");
  const recusar = h("button", { type: "button", class: "botao perigo-contorno" }, "Recusar");

  async function decidir(botao, tipo) {
    const responsavel = campoResponsavel();
    const motivo = h("textarea", { maxlength: 1000, rows: 3 });
    const resumo = `oferta ${oferta.id} de ${reais(c.valor_centavos)} para ${oferta.destinatario_cnpj}`;
    const ok = await confirmar(tipo === "aprovacao" ? {
      titulo: "Aprovar oferta",
      corpo: [`Você vai assinar a aprovação da ${resumo}, com a chave da organização.`,
              "Depois disso o embarcador pode aceitá-la pelo link enviado pelo agente."],
      botao: "Assinar aprovação",
      campos: [responsavel.rotulo],
    } : {
      titulo: "Recusar oferta",
      corpo: [`Você vai assinar a recusa da ${resumo}. A recusa é definitiva: a oferta não `
              + "poderá mais ser aprovada nem aceita."],
      botao: "Assinar recusa",
      perigo: true,
      campos: [responsavel.rotulo, campo("Motivo (opcional)", motivo)],
    });
    if (!ok) return;
    const nome = responsavel.entrada.value.trim();
    if (!nome) return avisar("Informe seu nome para assinar.", "erro");
    localStorage.setItem("elo.responsavel", nome);
    await executando(botao, async () => {
      try {
        const oferta_hash = await hashSha256(oferta);
        const base = `/ofertas/${encodeURIComponent(oferta.id)}`;
        if (tipo === "aprovacao") {
          await api.assinarEEnviar({
            rotaRascunho: `${base}/aprovacao/rascunho`, corpo: { aprovado_por: nome },
            rotaEnvio: `${base}/aprovacao`,
            esperado: { tipo: "aprovacao", oferta_hash, aprovado_por: nome },
          });
          avisar(`Oferta ${oferta.id} aprovada.`);
        } else {
          const texto = motivo.value.trim();
          await api.assinarEEnviar({
            rotaRascunho: `${base}/recusa/rascunho`, corpo: { recusado_por: nome, motivo: texto },
            rotaEnvio: `${base}/recusa`,
            esperado: { tipo: "recusa", oferta_hash, recusado_por: nome, motivo: texto },
          });
          avisar(`Oferta ${oferta.id} recusada.`);
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
      h("div", {}, h("h2", {}, reais(c.valor_centavos)), mono(oferta.id)),
      vencida ? h("span", { class: "etiqueta ruim" }, "Validade vencida") : null),
    h("div", { class: "caixa motivo" },
      h("h3", {}, "Por que precisa de aprovação"),
      h("ul", {}, violacoes.map((v) => h("li", {}, v)))),
    dados([
      ["Embarcador", mono(oferta.destinatario_cnpj)],
      ["Rota", rota(oferta.carga)],
      ["Carga", `${oferta.carga.tipo}, ${oferta.carga.peso_kg} kg, ${oferta.carga.volume_litros} L`],
      ["Coleta até", dataBr(c.coleta_ate)],
      ["Prazo de entrega", `${c.prazo_entrega_dias_uteis} dias úteis`],
      c.inclui.length ? ["Inclui", c.inclui.join(", ")] : null,
      ["Válida até", emBrasilia(oferta.valida_ate)],
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
    "Ofertas que o agente fez fora do mandato. Nada sai sem a sua assinatura."),
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
                                       placeholder: "32 bytes em base64" });
  const botao = h("button", { type: "submit", class: "botao primario" }, "Assinar certificado");
  const form = h("form", { class: "formulario cartao" },
    h("h2", {}, "Novo agente"),
    h("p", { class: "ajuda" }, "A chave privada do agente fica no sistema onde ele roda; cole "
      + "aqui só a chave pública. A organização assina um certificado ligando essa chave a ela."),
    campo("Nome", nome),
    campo("Identificador da chave", chaveId),
    campo("Chave pública Ed25519", chavePublica),
    h("div", { class: "acoes" }, botao));
  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    const pub = chavePublica.value.trim();
    if (!chavePublicaValida(pub)) {
      avisar("A chave pública precisa ter 32 bytes em base64 (Ed25519).", "erro");
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
        avisar(`Agente ${agente.nome} criado (${agente.agente_id}).`);
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
    "Cada agente tem um certificado assinado pela organização."),
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
      corpo: [`Você vai assinar a revogação do mandato ${mandato.id} de `
              + `${item.agente_nome ?? mandato.agente_id}.`,
              "É irreversível. A partir de agora, ofertas feitas com este mandato vão para "
              + "aprovação humana. Compromissos já firmados continuam válidos."],
      botao: "Assinar revogação",
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
        avisar(`Mandato ${mandato.id} revogado.`);
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
      ["Tipos de carga", l.tipos_carga.join(", ")],
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
  const tipos = h("textarea", { required: true, rows: 2, placeholder: "carga geral" });
  const agora = new Date();
  const de = h("input", { type: "datetime-local", required: true, value: localDeBrasilia(agora) });
  const ate = h("input", { type: "datetime-local", required: true,
                           value: localDeBrasilia(new Date(agora.getTime() + 30 * 864e5)) });
  const botao = h("button", { type: "submit", class: "botao primario" }, "Assinar mandato");

  const form = h("form", { class: "formulario cartao" },
    h("h2", {}, "Novo mandato"),
    h("p", { class: "ajuda" }, "O que o agente pode oferecer sozinho. Fora disso, a oferta "
      + "vem para a sua aprovação."),
    campo("Agente", agente),
    campo("Valor máximo por oferta (R$)", valor),
    origem, destino,
    campo("Tipos de carga", tipos, "Um por linha ou separados por vírgula."),
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
          tipos_carga: lista(tipos.value),
          valido_de: isoDeBrasilia(de.value),
          valido_ate: isoDeBrasilia(ate.value),
        };
        if (!limites.ufs_origem.length || !limites.ufs_destino.length) {
          throw new Error("Marque ao menos uma UF de origem e uma de destino.");
        }
        const mandato = await api.assinarEEnviar({
          rotaRascunho: "/mandatos/rascunho",
          corpo: { cnpj: atual.cnpj, agente_id: agente.value, limites },
          rotaEnvio: "/mandatos",
          esperado: { tipo: "mandato", cnpj: atual.cnpj, agente_id: agente.value, limites },
        });
        avisar(`Mandato ${mandato.id} emitido.`);
        recarregar();
      } catch (erro) {
        avisarErro(erro);
      }
    });
  });

  const ordenados = mandatos.sort((a, b) =>
    b.mandato.emitido_em.localeCompare(a.mandato.emitido_em));
  return h("section", {}, cabecalho("Mandatos",
    "Limites assinados pela organização para cada agente. Revogue se um agente sair do controle."),
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
      h("tbody", {}, compromissos.map(({ compromisso: cp, oferta, verificacao }) => h("tr", {},
        h("td", {}, emBrasilia(cp.aceite.em)),
        h("td", {}, mono(cp.aceite.cnpj), h("div", { class: "ajuda" }, cp.aceite.nome)),
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
