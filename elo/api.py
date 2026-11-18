"""API HTTP (FastAPI) sobre elo.servico.

Esta camada só traduz HTTP <-> chamadas da Plataforma: nenhuma regra de
negócio mora aqui. Erros de regra (ErroElo) viram 400, objetos inexistentes
(NaoEncontrado) viram 404 e corpo mal formado vira 422.

A API nunca recebe chave privada. Tudo que precisa da assinatura de uma
empresa segue dois passos:

    POST /<recurso>/rascunho   -> a plataforma devolve o objeto sem assinatura
    (o cliente assina localmente com a própria chave)
    POST /<recurso>            -> o cliente envia o objeto assinado

Subir:  uvicorn elo.api:app --reload
Banco:  variável ELO_DB (padrão: elo.db na pasta atual).
"""

from __future__ import annotations

import html
import os
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .canonico import hash_sha256
from .chaves import ParDeChaves
from .mandato import reais
from .modelos import Carga, Condicoes, LimitesMandato
from .registro import Registro
from .servico import ErroElo, NaoEncontrado, Plataforma, Relatorio

# ---------- corpos das requisições ----------


class _Corpo(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepararOrganizacao(_Corpo):
    cnpj: str
    razao_social: str = Field(min_length=1, max_length=200)
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Chave pública Ed25519, 32 bytes em base64")


class PrepararAgente(_Corpo):
    cnpj: str = Field(description="CNPJ da organização dona do agente")
    nome: str = Field(min_length=1, max_length=200)
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Chave pública Ed25519 do agente, em base64")


class PrepararMandato(_Corpo):
    cnpj: str
    agente_id: str
    limites: LimitesMandato


class RegistrarPedido(_Corpo):
    cnpj_solicitante: str
    canal: str = Field(min_length=1, max_length=40)
    carga: Carga
    mensagem_original: str = Field(default="", max_length=20_000)


class PrepararOferta(_Corpo):
    pedido_id: str
    agente_id: str
    mandato_id: str
    condicoes: Condicoes
    valida_ate: str = Field(description="ISO 8601 com fuso, ex.: 2026-11-18T18:00:00-03:00")


class PrepararAprovacao(_Corpo):
    aprovado_por: str = Field(min_length=1, max_length=200)


class Aceite(_Corpo):
    cnpj: str = Field(description="CNPJ do embarcador destinatário da oferta")
    nome: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)


ObjetoAssinado = Body(description="O rascunho devolvido pela plataforma, com o campo "
                                  "'assinatura' preenchido pelo cliente")


# ---------- aplicação ----------


def _plataforma_padrao() -> Plataforma:
    # A chave da plataforma (não a das empresas) é gerada a cada início e só
    # vive na memória. A chave pública vai para o registro, então aceites
    # atestados por chaves de execuções anteriores continuam verificáveis.
    chave = ParDeChaves.gerar(f"plataforma#{uuid.uuid4().hex[:8]}")
    return Plataforma(Registro(os.environ.get("ELO_DB", "elo.db")), chave)


def criar_app(plataforma: Plataforma | None = None) -> FastAPI:
    """Cria a API. Sem plataforma, abre o banco de ELO_DB ao iniciar."""

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
        propria = getattr(app.state, "plataforma", None) is None
        if propria:
            app.state.plataforma = _plataforma_padrao()
        yield
        if propria:  # fecha o banco que esta app abriu
            app.state.plataforma.registro.conexao.close()
            app.state.plataforma = None

    app = FastAPI(
        title="Elo",
        description="Camada de confiança entre agentes logísticos (V1). "
                    "Chaves privadas nunca passam pela API.",
        version="0.1",
        lifespan=ciclo_de_vida,
    )
    app.state.plataforma = plataforma
    # a Plataforma mantém estado em memória e uma conexão SQLite: uma
    # requisição por vez
    trava = threading.Lock()

    @app.exception_handler(NaoEncontrado)
    async def _nao_encontrado(_: Request, erro: NaoEncontrado) -> JSONResponse:
        return JSONResponse({"detail": str(erro)}, status_code=404)

    @app.exception_handler(ErroElo)
    async def _erro_elo(_: Request, erro: ErroElo) -> JSONResponse:
        return JSONResponse({"detail": str(erro)}, status_code=400)

    def plat() -> Plataforma:
        return app.state.plataforma

    # ----- organização -----

    @app.post("/organizacoes/rascunho", tags=["organização"])
    def preparar_organizacao(corpo: PrepararOrganizacao) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_organizacao(
                corpo.cnpj, corpo.razao_social, corpo.chave_id, corpo.chave_publica)}

    @app.post("/organizacoes", status_code=201, tags=["organização"])
    def cadastrar_organizacao(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "organizacao")

    # ----- agente -----

    @app.post("/agentes/rascunho", tags=["agente"])
    def preparar_agente(corpo: PrepararAgente) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_agente(
                corpo.cnpj, corpo.nome, corpo.chave_id, corpo.chave_publica)}

    @app.post("/agentes", status_code=201, tags=["agente"])
    def emitir_agente(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "certificado_agente")

    # ----- mandato -----

    @app.post("/mandatos/rascunho", tags=["mandato"])
    def preparar_mandato(corpo: PrepararMandato) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_mandato(
                corpo.cnpj, corpo.agente_id, corpo.limites)}

    @app.post("/mandatos", status_code=201, tags=["mandato"])
    def emitir_mandato(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "mandato")

    # ----- pedido (não precisa de assinatura) -----

    @app.post("/pedidos", status_code=201, tags=["pedido"])
    def registrar_pedido(corpo: RegistrarPedido) -> dict[str, Any]:
        with trava:
            return plat().registrar_pedido(corpo.cnpj_solicitante, corpo.canal,
                                           corpo.carga, corpo.mensagem_original)

    # ----- oferta -----

    @app.post("/ofertas/rascunho", tags=["oferta"])
    def preparar_oferta(corpo: PrepararOferta) -> dict[str, Any]:
        with trava:
            rascunho, problemas = plat().preparar_oferta(
                corpo.pedido_id, corpo.agente_id, corpo.mandato_id,
                corpo.condicoes, corpo.valida_ate)
        return {"rascunho": rascunho, "violacoes": problemas,
                "status_previsto": "aguardando_aprovacao" if problemas else "enviada"}

    @app.post("/ofertas", status_code=201, tags=["oferta"])
    def emitir_oferta(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        """Devolve {"oferta", "status", "violacoes"}."""
        with trava:
            return plat().registrar_assinado(assinado, "oferta")

    @app.post("/ofertas/{oferta_id}/aprovacao/rascunho", tags=["oferta"])
    def preparar_aprovacao(oferta_id: str, corpo: PrepararAprovacao) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_aprovacao(oferta_id, corpo.aprovado_por)}

    @app.post("/ofertas/{oferta_id}/aprovacao", status_code=201, tags=["oferta"])
    def aprovar_oferta(oferta_id: str,
                       assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            oferta = plat()._oferta(oferta_id)  # 404 se não existir
            if assinado.get("oferta_hash") != hash_sha256(oferta):
                raise ErroElo("a aprovação enviada é de outra oferta")
            return plat().registrar_assinado(assinado, "aprovacao")

    @app.post("/ofertas/{oferta_id}/aceite", status_code=201, tags=["oferta"])
    def aceitar_oferta(oferta_id: str, corpo: Aceite) -> dict[str, Any]:
        """Aceite humano pelo link: a plataforma atesta com a chave dela."""
        with trava:
            return plat().aceitar_oferta(oferta_id, corpo.cnpj, corpo.nome, corpo.email)

    # ----- verificação -----

    @app.get("/compromissos/{compromisso_id}/verificacao", tags=["verificação"])
    def verificar_compromisso(compromisso_id: str) -> JSONResponse:
        with trava:
            relatorio = plat().verificar_compromisso(compromisso_id)
        return JSONResponse(_relatorio_json(relatorio),
                            status_code=200 if _existe(relatorio) else 404)

    @app.get("/verificar/{compromisso_id}", response_class=HTMLResponse,
             tags=["verificação"])
    def pagina_verificacao(compromisso_id: str) -> HTMLResponse:
        """Página pública, em português, com as 14 checagens e o resultado."""
        with trava:
            relatorio = plat().verificar_compromisso(compromisso_id)
            resumo = _resumo(plat(), compromisso_id)
        return HTMLResponse(_pagina(relatorio, resumo),
                            status_code=200 if _existe(relatorio) else 404)

    return app


# ---------- apresentação ----------


def _existe(relatorio: Relatorio) -> bool:
    return any(v.nome == "Compromisso existe" and v.ok for v in relatorio.verificacoes)


def _relatorio_json(relatorio: Relatorio) -> dict[str, Any]:
    return {
        "compromisso_id": relatorio.compromisso_id,
        "valido": relatorio.valido,
        "verificacoes": [{"nome": v.nome, "ok": v.ok, "detalhe": v.detalhe}
                         for v in relatorio.verificacoes],
    }


def _resumo(plataforma: Plataforma, compromisso_id: str) -> list[tuple[str, str]]:
    """Dados do acordo para a página. Só leitura do estado; sem regra de negócio.

    Nome e e-mail de quem aceitou ficam fora da página pública.
    """
    cp = plataforma.compromissos.get(compromisso_id)
    if cp is None:
        return []
    linhas = [("Aceite em", cp["aceite"]["em"]), ("Embarcador", cp["aceite"]["cnpj"])]
    oferta_id = plataforma.ofertas_por_hash.get(cp["oferta_hash"])
    oferta = plataforma.ofertas.get(oferta_id) if oferta_id else None
    if oferta is not None:
        cnpj = oferta["emissor"]["cnpj"]
        org = plataforma.organizacoes.get(cnpj)
        carga, cond = oferta["carga"], oferta["condicoes"]
        linhas = [
            ("Transportadora", f"{org['razao_social']} ({cnpj})" if org else cnpj),
            *linhas,
            ("Oferta", oferta["id"]),
            ("Valor", reais(cond["valor_centavos"])),
            ("Rota", f"{carga['origem_uf']} ({carga['origem_cep']}) → "
                     f"{carga['destino_uf']} ({carga['destino_cep']})"),
            ("Carga", f"{carga['tipo']}, {carga['peso_kg']} kg, "
                      f"{carga['volume_litros']} L"),
            ("Coleta até", cond["coleta_ate"]),
            ("Prazo de entrega", f"{cond['prazo_entrega_dias_uteis']} dias úteis"),
        ]
    linhas.append(("Hash da oferta", cp["oferta_hash"]))
    return linhas


_ESTILO = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
background:#f6f7f9;color:#1d2330}
main{max-width:760px;margin:0 auto;padding:32px 20px 48px}
h1{font-size:1.4rem;margin:0 0 4px}
.id{font-family:ui-monospace,Consolas,monospace;color:#5b6475;margin:0 0 24px;
word-break:break-all}
.resultado{border-radius:10px;padding:16px 20px;font-size:1.25rem;font-weight:700;
margin-bottom:24px}
.valido{background:#e3f4e8;color:#14532d;border:1px solid #9fd5b0}
.invalido{background:#fbe4e4;color:#7f1d1d;border:1px solid #eba9a9}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;
overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.06);margin-bottom:24px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #eceef2;
vertical-align:top}
th{background:#f0f2f5;font-size:.85rem;color:#5b6475}
tr:last-child td{border-bottom:0}
.n{color:#8a93a3;width:2em}
.st{font-weight:700;white-space:nowrap;width:5em}
.ok{color:#15803d}.falha{color:#b91c1c}
.det{color:#5b6475;font-size:.9rem}
.mono{font-family:ui-monospace,Consolas,monospace;word-break:break-all}
footer{color:#5b6475;font-size:.85rem;line-height:1.5}
"""


def _pagina(relatorio: Relatorio, resumo: list[tuple[str, str]]) -> str:
    e = html.escape
    valido = relatorio.valido
    linhas = "".join(
        f'<tr><td class="n">{i}</td><td>{e(v.nome)}'
        + (f'<div class="det">{e(v.detalhe)}</div>' if v.detalhe else "")
        + f'</td><td class="st {"ok" if v.ok else "falha"}">'
          f'{"OK" if v.ok else "FALHA"}</td></tr>'
        for i, v in enumerate(relatorio.verificacoes, 1)
    )
    tabela_resumo = ""
    if resumo:
        tabela_resumo = (
            '<h2>O que foi combinado</h2><table>'
            + "".join(f'<tr><th>{e(k)}</th><td class="{"mono" if k.startswith("Hash") else ""}">'
                      f'{e(v)}</td></tr>' for k, v in resumo)
            + "</table>")
    ok_total = sum(v.ok for v in relatorio.verificacoes)
    cid = e(relatorio.compromisso_id)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Verificação do compromisso {cid} · Elo</title>
<style>{_ESTILO}</style>
</head>
<body>
<main>
<h1>Verificação do compromisso</h1>
<p class="id">{cid}</p>
<div class="resultado {"valido" if valido else "invalido"}">
{"✔ COMPROMISSO VÁLIDO" if valido else "✘ COMPROMISSO INVÁLIDO"}
<div class="det">{ok_total} de {len(relatorio.verificacoes)} checagens OK</div>
</div>
<h2>Checagens</h2>
<table>
<tr><th class="n">#</th><th>Checagem</th><th class="st">Resultado</th></tr>
{linhas}
</table>
{tabela_resumo}
<footer>
<p>Todas as checagens são refeitas agora, a partir do registro de eventos com
hashes encadeados. As assinaturas são Ed25519. O aceite do embarcador foi
feito por link e é atestado pela plataforma, não assinado pelo embarcador.</p>
<p><a href="/compromissos/{cid}/verificacao">Ver relatório em JSON</a></p>
</footer>
</main>
</body>
</html>"""


app = criar_app()
