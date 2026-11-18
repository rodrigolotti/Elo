// Telas da chave da organização: arquivo de recuperação, aba Segurança e a
// recuperação em outro navegador (rotação da chave principal).

import * as api from "./api.js";
import { assinar, chavePublicaBase64, gerarParDeChaves } from "./assinatura.js";
import { apagarChave, lerChave, pedirArmazenamentoPersistente, substituirChave } from "./cofre.js";
import { conferirRascunho } from "./conferencia.js";
import { anexar, h, limpar } from "./dom.js";
import {
  abrirArquivoRecuperacao, criarArquivoRecuperacao, gerarFraseSenha, nomeDoArquivo,
} from "./recuperacao.js";
import {
  avisar, avisarErro, baixarJson, cabecalho, campo, confirmar, executando, mono,
} from "./ui.js";

const hoje = () => new Date().toISOString().slice(0, 10);

/** Mesma normalização da frase (O vira 0; I e L viram 1), por grupo de 5. */
const normalizarGrupo = (texto) => String(texto).toUpperCase().replace(/[\s-]/g, "")
  .replaceAll("O", "0").replace(/[IL]/g, "1");

function sortearDoisGrupos() {
  const [a, b] = crypto.getRandomValues(new Uint8Array(2));
  const primeiro = a % 5;
  const segundo = (primeiro + 1 + (b % 4)) % 5;
  return [primeiro, segundo].sort((x, y) => x - y);
}

function erroNaTela(texto) {
  return h("div", { class: "caixa erro", role: "alert" }, texto);
}

// ---------- passo: gerar, mostrar a frase, baixar e confirmar ----------

/**
 * Gera a chave de recuperação e conduz a pessoa: anotar a frase-senha, baixar
 * o arquivo e digitar dois grupos sorteados da frase. Sem acertar, não conclui.
 * Devolve { elemento, pronto } (pronto resolve com { chaveId, chavePublica }).
 */
export function passoArquivoRecuperacao({ cnpj }) {
  const elemento = h("section", { class: "cartao passo-recuperacao" },
    h("p", { class: "carregando" }, "Gerando a chave de recuperação…"));
  const pronto = (async () => {
    const chaveId = `recuperacao-${String(cnpj).replace(/\D/g, "")}#${hoje()}`;
    let frase = gerarFraseSenha();
    const { arquivo, chavePublica } = await criarArquivoRecuperacao({ cnpj, chaveId }, frase);
    const grupos = frase.split("-");
    const [g1, g2] = sortearDoisGrupos();

    return new Promise((resolve) => {
      let baixou = false;

      function mostrarFrase() {
        const continuar = h("button", { type: "button", class: "botao primario", disabled: !baixou },
          "Já anotei a frase e guardei o arquivo");
        const baixar = h("button", { type: "button", class: "botao secundario" },
          "Baixar arquivo de recuperação");
        baixar.addEventListener("click", () => {
          baixarJson(nomeDoArquivo(cnpj), arquivo);
          baixou = true;
          continuar.disabled = false;
        });
        continuar.addEventListener("click", pedirGrupos);
        limpar(elemento).append(
          h("h2", {}, "Chave de recuperação"),
          h("p", {}, "Se a chave deste navegador se perder, é com o arquivo de recuperação e "
            + "esta frase-senha que a organização troca de chave. Ela aparece só agora."),
          h("div", { class: "frase-senha", "aria-label": "Frase-senha" }, frase),
          h("div", { class: "caixa alerta" },
            h("p", {}, h("strong", {}, "Anote a frase-senha em papel e guarde separada do arquivo.")),
            h("ul", {},
              h("li", {}, "Quem tiver o arquivo e a frase toma o controle da organização."),
              h("li", {}, "Sem a frase, o arquivo não abre. O Elo não guarda nenhum dos dois "
                + "e não consegue recuperá-los."),
              h("li", {}, "Não fotografe a frase nem a guarde no mesmo lugar do arquivo."))),
          h("div", { class: "acoes" }, baixar, continuar));
      }

      function pedirGrupos() {
        const entrada1 = h("input", { autocomplete: "off", class: "mono", maxlength: 7,
                                      "data-grupo": g1 + 1 });
        const entrada2 = h("input", { autocomplete: "off", class: "mono", maxlength: 7,
                                      "data-grupo": g2 + 1 });
        const erro = h("div", {});
        const confirmarBotao = h("button", { type: "submit", class: "botao primario" }, "Confirmar");
        const form = h("form", { class: "formulario" },
          h("h2", {}, "Confirme que você anotou a frase"),
          h("p", {}, `Digite o ${g1 + 1}º e o ${g2 + 1}º grupo da frase-senha, como anotou.`),
          h("div", { class: "grupos" },
            campo(`${g1 + 1}º grupo`, entrada1), campo(`${g2 + 1}º grupo`, entrada2)),
          erro,
          h("div", { class: "acoes" },
            h("button", { type: "button", class: "botao secundario", onclick: mostrarFrase },
              "Ver a frase de novo"),
            confirmarBotao));
        form.addEventListener("submit", (evento) => {
          evento.preventDefault();
          if (normalizarGrupo(entrada1.value) !== grupos[g1]
              || normalizarGrupo(entrada2.value) !== grupos[g2]) {
            limpar(erro).append(erroNaTela(
              "Os grupos não conferem com a frase-senha. Confira o que você anotou; sem isso, "
              + "não dá para concluir."));
            return;
          }
          frase = null; // melhor esforço: a frase não fica guardada em lugar nenhum
          grupos.fill("");
          limpar(elemento).append(h("p", { class: "carregando" }, "Registrando a chave de recuperação…"));
          resolve({ chaveId, chavePublica });
        });
        limpar(elemento).append(form);
        entrada1.focus();
      }

      mostrarFrase();
    });
  })();
  pronto.catch((erro) => {
    limpar(elemento).append(erroNaTela(erro.message));
  });
  return { elemento, pronto };
}

/** Registra a chave de recuperação (assinada pela chave principal da sessão). */
async function registrarRecuperacao({ chaveId, chavePublica }) {
  const { cnpj } = api.sessao();
  return api.assinarEEnviar({
    rotaRascunho: "/organizacoes/recuperacao/rascunho",
    corpo: { cnpj, chave_id: chaveId, chave_publica: chavePublica },
    rotaEnvio: "/organizacoes/recuperacao",
    esperado: { tipo: "recuperacao_chave", cnpj, chave_id: chaveId, chave_publica: chavePublica },
  });
}

/** Passo 2 do cadastro: a organização acabou de entrar e ainda não tem recuperação. */
export function telaRecuperacaoDoCadastro(conteudo) {
  const passo = passoArquivoRecuperacao({ cnpj: api.sessao().cnpj });
  limpar(conteudo).append(h("section", {},
    cabecalho("Guarde a chave de recuperação",
      "Passo 2 de 2. A organização já está cadastrada; falta o que permite recuperar a chave."),
    passo.elemento));
  passo.pronto.then(async (gerada) => {
    try {
      await registrarRecuperacao(gerada);
      avisar("Chave de recuperação registrada. Guarde o arquivo e a frase em lugares separados.");
      location.hash = "#/agentes";
    } catch (erro) {
      avisarErro(erro);
      location.hash = "#/seguranca";
    }
  }, () => {});
}

// ---------- aba Segurança ----------

export async function telaSeguranca() {
  const resumo = await api.obter("/organizacao");
  const { chave_atual: atual, recuperacao, historico_chaves: historico } = resumo;
  const secao = h("section", {}, cabecalho("Segurança",
    "As chaves da organização. A principal fica só neste navegador; a de recuperação, "
    + "cifrada no arquivo que você guardou."));

  const area = h("div", {});
  const gerar = h("button", { type: "button", class: "botao primario" },
    recuperacao ? "Gerar nova chave de recuperação" : "Criar chave de recuperação");
  gerar.addEventListener("click", async () => {
    if (recuperacao) {
      const ok = await confirmar({
        titulo: "Gerar nova chave de recuperação",
        corpo: ["O arquivo de recuperação atual e a frase dele deixam de valer.",
                "Você vai baixar um arquivo novo e anotar uma frase nova."],
        botao: "Gerar nova",
      });
      if (!ok) return;
    }
    gerar.disabled = true;
    const passo = passoArquivoRecuperacao({ cnpj: api.sessao().cnpj });
    limpar(area).append(passo.elemento);
    try {
      await registrarRecuperacao(await passo.pronto);
      avisar("Nova chave de recuperação registrada. O arquivo anterior não vale mais.");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } catch (erro) {
      avisarErro(erro);
      gerar.disabled = false;
    }
  });

  anexar(secao, [
    recuperacao ? null : h("div", { class: "caixa alerta" },
      h("p", {}, h("strong", {}, "A organização não tem chave de recuperação."),
        " Se a chave deste navegador se perder, não haverá como recuperá-la.")),
    h("div", { class: "lista-cartoes" },
      h("article", { class: "cartao" },
        h("h2", {}, "Chaves"),
        h("dl", { class: "dados" },
          h("dt", {}, "Chave principal"), h("dd", {}, mono(atual.chave_id)),
          h("dt", {}, "Chave de recuperação"),
          h("dd", {}, recuperacao ? mono(recuperacao.chave_id) : "nenhuma")),
        h("div", { class: "acoes" }, gerar)),
      area,
      h("article", { class: "cartao" },
        h("h2", {}, "Histórico de chaves"),
        historico.length === 0 ? h("p", { class: "ajuda" }, "Chave original, sem eventos de chave.")
          : h("table", {},
            h("thead", {}, h("tr", {}, ["Evento", "Registro", "Situação"].map((t) => h("th", {}, t)))),
            h("tbody", {}, historico.map((e) => h("tr", {},
              h("td", {}, e.evento), h("td", {}, mono(`#${e.seq}`)),
              h("td", { class: e.valido ? "ok" : "falha" },
                e.valido ? "OK" : `FALHA: ${e.motivo}`)))))))]);
  return secao;
}

// ---------- recuperar em outro navegador ----------

function lerArquivo(arquivo) {
  return new Promise((resolve, reject) => {
    const leitor = new FileReader();
    leitor.onload = () => resolve(leitor.result);
    leitor.onerror = () => reject(new Error("não foi possível ler o arquivo"));
    leitor.readAsText(arquivo);
  });
}

async function revogarMandatosAtivos() {
  const ativos = (await api.obter("/mandatos")).filter((m) => !m.revogado_em);
  for (const { mandato } of ativos) {
    const base = `/mandatos/${encodeURIComponent(mandato.id)}/revogacao`;
    await api.assinarEEnviar({ rotaRascunho: `${base}/rascunho`, corpo: {}, rotaEnvio: base,
                               esperado: { tipo: "revogacao", mandato_id: mandato.id } });
  }
  return ativos.length;
}

export async function telaRecuperar(conteudo) {
  const arquivo = h("input", { type: "file", accept: ".json,application/json", required: true });
  const frase = h("input", { required: true, autocomplete: "off", class: "mono", name: "frase",
                             placeholder: "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" });
  const erro = h("div", {});
  const botao = h("button", { type: "submit", class: "botao primario" }, "Abrir arquivo");
  const form = h("form", { class: "formulario cartao" },
    h("h2", {}, "1. Abrir o arquivo de recuperação"),
    h("p", { class: "ajuda" }, "O arquivo é aberto aqui, no navegador. Nem ele nem a frase "
      + "vão para o servidor."),
    campo("Arquivo de recuperação (.json)", arquivo),
    campo("Frase-senha", frase, "25 letras e números, em 5 grupos."),
    erro,
    h("div", { class: "acoes" }, h("a", { class: "botao secundario", href: "#/entrar" }, "Voltar"),
      botao));

  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    limpar(erro);
    executando(botao, async () => {
      try {
        if (!arquivo.files[0]) throw new Error("escolha o arquivo de recuperação");
        let conteudoArquivo;
        try {
          conteudoArquivo = JSON.parse(await lerArquivo(arquivo.files[0]));
        } catch {
          throw new Error("este arquivo não é um arquivo de recuperação do Elo");
        }
        const aberto = await abrirArquivoRecuperacao(conteudoArquivo, frase.value);
        frase.value = "";
        continuarRecuperacao(conteudo, aberto);
      } catch (e) {
        limpar(erro).append(erroNaTela(e.message.charAt(0).toUpperCase() + e.message.slice(1)));
      }
    });
  });

  return h("section", {}, cabecalho("Recuperar a chave da organização",
    "Para quando a chave deste navegador se perdeu ou você está em outro computador. "
    + "A organização ganha uma chave principal nova e uma chave de recuperação nova."),
  form);
}

function continuarRecuperacao(conteudo, aberto) {
  const { cnpj } = aberto;
  const passo = passoArquivoRecuperacao({ cnpj });
  const revogar = h("input", { type: "checkbox", checked: true, name: "revogar" });
  const trocar = h("button", { type: "button", class: "botao perigo" },
    "Trocar a chave da organização");
  const erro = h("div", {});
  const final = h("section", { class: "cartao formulario", hidden: true },
    h("h2", {}, "3. Trocar a chave"),
    h("label", { class: "confirmacao" }, revogar,
      h("span", {}, "Revogar todos os mandatos ativos (recomendado). ",
        h("span", { class: "ajuda" }, "Se a chave antiga pode ter sido roubada, e não só "
          + "perdida, os agentes não continuam autorizados por mandatos que ela assinou. "
          + "Depois, emita mandatos novos."))),
    erro,
    h("div", { class: "acoes" }, trocar));

  limpar(conteudo).append(h("section", {},
    cabecalho("Recuperar a chave da organização", `Organização ${cnpj}. Arquivo aberto.`),
    h("h2", { class: "etapa" }, "2. Nova chave de recuperação"),
    h("p", { class: "ajuda" }, "O arquivo que você usou deixa de valer depois da troca. "
      + "Guarde o novo antes de continuar."),
    passo.elemento, final));

  passo.pronto.then((novaRecuperacao) => {
    limpar(passo.elemento).append(h("p", {}, "✔ Novo arquivo de recuperação guardado e frase confirmada."));
    final.hidden = false;
    trocar.addEventListener("click", () => executando(trocar, async () => {
      limpar(erro);
      const anterior = await lerChave(cnpj);
      let gravouNova = false;
      try {
        const par = await gerarParDeChaves();
        const chavePublica = await chavePublicaBase64(par.publicKey);
        const chaveId = `org-${cnpj.replace(/\D/g, "")}#${hoje()}-${Date.now() % 100000}`;
        const corpo = { cnpj, chave_id: chaveId, chave_publica: chavePublica,
                        recuperacao_chave_id: novaRecuperacao.chaveId,
                        recuperacao_chave_publica: novaRecuperacao.chavePublica };
        const { rascunho } = await api.enviar("/organizacoes/rotacao/rascunho", corpo);
        conferirRascunho(rascunho, {
          tipo: "rotacao_chave", cnpj,
          chave_nova: { chave_id: chaveId, chave_publica: chavePublica },
          recuperacao_nova: { chave_id: novaRecuperacao.chaveId,
                              chave_publica: novaRecuperacao.chavePublica },
          recuperacao_usada: { chave_id: aberto.chaveId, chave_publica: aberto.chavePublica },
        });
        // a chave nova vai para o cofre ANTES de a troca valer; se a troca
        // falhar, o que havia antes neste navegador volta
        const registro = { cnpj, razaoSocial: anterior?.razaoSocial ?? cnpj, chaveId, par,
                           chavePublica, privada: par.privateKey };
        await substituirChave(registro);
        gravouNova = true;
        await api.enviar("/organizacoes/rotacao",
          await assinar(rascunho, aberto.chavePrivada, aberto.chaveId));
        gravouNova = false; // valeu: fica

        await pedirArmazenamentoPersistente();
        await api.entrar(registro);
        const { cadastro } = await api.obter("/organizacao");
        await substituirChave({ ...registro, razaoSocial: cadastro.razao_social });
        api.definirRazaoSocial(cadastro.razao_social);
        let revogados = 0;
        if (revogar.checked) revogados = await revogarMandatosAtivos();
        avisar("Chave da organização trocada. A chave antiga não entra nem assina mais."
          + (revogar.checked ? ` ${revogados} mandato(s) revogado(s).` : ""));
        location.hash = "#/mandatos";
      } catch (e) {
        if (gravouNova) {
          await (anterior ? substituirChave(anterior) : apagarChave(cnpj)).catch(() => {});
        }
        limpar(erro).append(erroNaTela(e.message));
      }
    }));
  }, () => {});
}

/** Na tela de entrada: a chave guardada aqui já não é a da organização? */
export function mensagemChaveAntiga(erro) {
  if (erro?.status === 401 && /não confere/.test(erro.message)) {
    return "A chave guardada neste navegador não é mais a chave da organização: ela foi "
      + "trocada por uma recuperação. Para entrar aqui, use o arquivo de recuperação.";
  }
  return erro?.message ?? String(erro);
}
