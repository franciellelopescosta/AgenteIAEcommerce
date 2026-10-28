import traceback
import asyncio
import httpx
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


# Metadado de tags exibido no topo da documentação Swagger/ReDoc.
tags_metadata = [
    {
        "name": "Webhook",
        "description": "Endpoints de webhook para integração com a plataforma de atendimento conversacional (Vonix). "
        "Suporta fluxo síncrono (200) e assíncrono via callback (202 + POST na `callback_url`).",
    },
    {
        "name": "Health",
        "description": "Verificação de saúde (liveness) do serviço.",
    },
]

app_description = """
Serviço de Agente CEP — Callback Integration
=============================================

Microsserviço de triagem e validação de endereços via CEP, integrado ao **ViaCEP** (consulta de CEP)
e ao **Google Gemini** (resposta estruturada em linguagem natural). Projetado para se integrar a
plataformas de atendimento conversacional (como o Vonix) em **modo síncrono ou assíncrono (callback)**.

## Como funciona

O agente recebe o turno (histórico + mensagem atual), extrai o CEP, consulta o ViaCEP e usa o Gemini
para compor a resposta. A presença do campo `callback_url` define o fluxo:

- **Fluxo assíncrono (202 + callback):** quando o payload traz `callback_url`, o agente responde
  `202 Accepted` em milissegundos e processa a IA em background. Ao final, faz um `POST` do resultado
  para a `callback_url` fornecida (uso único, sem header de autorização).
- **Fluxo síncrono (200):** quando `callback_url` está ausente, o agente processa e devolve o resultado
  no próprio corpo da resposta. Útil para testes e plataformas que esperam resposta imediata.

## Regras de ouro da callback_url

- É uma **credencial única de uso único** por turno.
- **Sem header de autorização** — a própria URL autoriza o envio.
- **Não deve ser persistida em logs** — use, entregue, descarte.
- Segunda chamada retorna **409** (turno já encerrado: `already_resolved`, `expired` ou `chat_closed`).
- O token final da URL **não é montado pelo agente** — use-a exatamente como recebida.

## Ciclo de vida do turno (fluxo assíncrono)

1. **`POST /webhook/callback`** com `callback_url` → resposta `202 Accepted` imediata.
2. Processamento em background (ViaCEP + Gemini).
3. **`POST {callback_url}`** com o resultado no formato Vonix (messages/tags/variables/exit).
4. A plataforma valida e aplica. Em caso de erro não retratável (404/409/400/413), o turno segue
   encerrado e o chat cai na saída **Fallback** do bloco.

## Integração

- **Vonix:** resposta estruturada em `{messages, tags, variables, exit}`. `exit.tool` deve bater com
  o `id` de uma ferramenta configurada no bloco. `variables` somente grava chaves listadas em
  `return_variables` (montado em Python, fora do schema do Gemini).
- **ViaCEP:** `GET https://viacep.com.br/ws/{cep}/json/`.
- **Google Gemini:** structured output via `response_schema` (modelos tentados em fallback:
  `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`).
"""

app = FastAPI(
    title="Agente CEP - Callback",
    description=app_description,
    version="1.0.0",
    contact={
        "name": "Fake Atendimento E-commerce",
        "url": "https://github.com/franciellelopescosta/AgenteIAEcommerce",
    },
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
)

client = genai.Client()

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"


class Contact(BaseModel):
    name: Optional[str] = Field(default=None, description="Nome completo do contato", examples=["Maria da Silva"])
    phone: Optional[str] = Field(default=None, description="Número de telefone com DDI e DDD", examples=["5567999999999"])
    user_id: Optional[str] = Field(default=None, description="Identificador único do usuário no sistema externo", examples=["usr_12345"])

    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "Maria da Silva", "phone": "5567999999999", "user_id": "usr_12345"}
    })


class IncomingMessage(BaseModel):
    turn_id: Optional[str] = Field(default=None, description="Identificador único do turno atual", examples=["turn_uuid_abc123"])
    chat_id: Optional[int] = Field(default=None, description="ID numérico do chat na plataforma", examples=[987654321])
    node_id: Optional[str] = Field(default=None, description="Identificador do bloco/nó atual no fluxo", examples=["node_cep"])
    channel: Optional[str] = Field(default=None, description="Canal de origem do atendimento (whatsapp, web, etc.)", examples=["whatsapp"])
    is_test: Optional[bool] = Field(default=False, description="Indica se é um turno de teste (não exige resposta real ao cliente)", examples=[False])
    contact: Optional[Contact] = Field(default=None, description="Dados do contato que iniciou o turno")
    text: Optional[str] = Field(default="", description="Texto do turno (mensagens do turno unidas por quebra de linha)", examples=["Qual o CEP 79002-270?"])
    messages: Optional[List[Dict[str, Any]]] = Field(default=[], description="Mensagens novas enviadas pelo contato neste turno", examples=[[{"type": "TEXT", "text": "Qual o CEP 79002-270?"}]])
    history: Optional[List[Dict[str, Any]]] = Field(default=[], description="Histórico de mensagens anteriores do chat (para contexto da IA)", examples=[[{"role": "contact", "text": "Olá"}, {"role": "agent", "text": "Olá! Como posso ajudar?"}]])
    variables: Optional[Dict[str, Any]] = Field(default={}, description="Variáveis de contexto ativas no fluxo", examples=[{"protocolo": "A-991"}])
    return_variables: Optional[List[str]] = Field(default=[], description="Lista de variáveis que o agente tem permissão de gravar (ex.: 'note')", examples=[["note"]])
    tools: Optional[List[Dict[str, Any]]] = Field(default=[], description="Lista de ferramentas/saídas permitidas neste turno (use o campo `id` da ferramenta em `exit.tool`)", examples=[[{"id": "tool_apos_cep", "name": "Apos informar cep", "type": "transfer", "description": "Finalizar atendimento após confirmar endereço"}]])
    callback_url: Optional[str] = Field(
        default=None,
        description="URL de callback exclusiva (token único) para entrega assíncrona. Quando presente, ativa o fluxo 202. Use-a exatamente como recebida — o token final não é montado pelo agente.",
        examples=["https://api.plataforma.com/chats/agent/callback/4f7a2c1e-9b3d-4c2a-8e1f-7a6b5c4d3e2f"],
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "turn_id": "turn-8842",
            "chat_id": 1023,
            "contact": {"name": "Maria", "phone": "5511999990000"},
            "text": "Vocês entregam no CEP 79002-270?",
            "messages": [{"type": "TEXT", "text": "Vocês entregam no CEP 79002-270?"}],
            "history": [],
            "variables": {},
            "return_variables": ["note"],
            "tools": [
                {"id": "tool_apos_cep", "name": "Apos informar cep", "type": "transfer", "description": "Finalizar após confirmar endereço"},
                {"id": "tool_cep_invalido", "name": "cep nao localizado", "type": "transfer", "description": "CEP não encontrado"}
            ],
            "callback_url": "https://api.vonix.com/flows/turn/4f7a2c1e-9b3d-4c2a-8e1f-7a6b5c4d3e2f/callback"
        }
    })


# Modelos de resposta expostos no Swagger (formato Vonix que a plataforma espera de volta).
# São distintos dos schemas internos usados pelo Gemini (GeminiAgentResponse), que usam
# messages: List[str] (texto puro). A conversão List[str] -> List[{type, text}] acontece em Python.
class ResponseMessage(BaseModel):
    type: str = Field(description="Tipo da mensagem (sempre 'TEXT' para texto)", examples=["TEXT"])
    text: str = Field(description="Conteúdo textual da mensagem a ser enviada ao contato", examples=["Boa, Maria! O CEP 79002-270 corresponde a Campo Grande/MS."])

    model_config = ConfigDict(json_schema_extra={
        "example": {"type": "TEXT", "text": "Boa, Maria! O CEP 79002-270 corresponde a Campo Grande/MS."}
    })


class ResponseExit(BaseModel):
    tool: Optional[str] = Field(default=None, description="ID da ferramenta de saída (deve bater com o `id` de uma tool enviada no payload)", examples=["tool_apos_cep"])
    note: Optional[str] = Field(default=None, description="Resumo do caso, injetado na variável `note` quando solicitado via `return_variables`", examples=["CEP 79002-270 confirmado, entrega disponível"])

    model_config = ConfigDict(json_schema_extra={
        "example": {"tool": "tool_apos_cep", "note": "CEP 79002-270 confirmado, entrega disponível"}
    })


class AgentResponse(BaseModel):
    messages: List[ResponseMessage] = Field(description="Mensagens a enviar ao contato (máx. 10 itens, na ordem de exibição). Mensagens vazias são filtradas para evitar erros no WhatsApp.")
    tags: Optional[List[str]] = Field(default=[], description="Tags a marcar no atendimento. Tag vazia invalida toda a resposta.", examples=[["consulta_cep", "endereco_confirmado"]])
    variables: Optional[Dict[str, Any]] = Field(default=None, description="Variáveis a gravar no fluxo (somente chaves listadas em `return_variables` são salvas; valores devem ser string/número/booleano — null/lista/objeto invalidam)", examples=[{"note": "CEP 79002-270 confirmado"}])
    exit: Optional[ResponseExit] = Field(default=None, description="Saída do bloco. Null = o cliente continua no mesmo bloco conversando.")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "messages": [{"type": "TEXT", "text": "Boa, Maria! O CEP 79002-270 corresponde a Campo Grande/MS. Entregamos normalmente na região. Precisa de mais alguma coisa?"}],
            "tags": ["consulta_cep", "endereco_confirmado"],
            "exit": None
        }
    })


class ErrorResponse(BaseModel):
    detail: str = Field(description="Descrição do erro ocorrido", examples=["Internal Server Error"])

    model_config = ConfigDict(json_schema_extra={"example": {"detail": "Internal Server Error"}})


# Schema interno usado pelo Gemini para gerar a resposta estruturada.
# IMPORTANTE: o campo "variables" NÃO pode estar neste schema — Dict[str, Any]
# é incompatível com o structured output do Gemini (INVALID_ARGUMENT).
# Ele é montado em Python, depois da resposta, a partir de return_variables.
# Estes modelos NÃO são expostos no Swagger (uso interno apenas).
class GeminiExit(BaseModel):
    tool: Optional[str] = Field(default=None, description="ID da ferramenta de saída (tool.id)")
    note: Optional[str] = Field(default=None, description="Resumo do caso para transferência ou encerramento")


class GeminiAgentResponse(BaseModel):
    messages: List[str] = Field(description="Lista de textos para enviar ao contato")
    tags: Optional[List[str]] = Field(default=[], description="Tags para marcar no atendimento")
    exit: Optional[GeminiExit] = Field(default=None, description="Saída do bloco. Null para continuar conversando")


def extrair_cep(texto: str) -> Optional[str]:
    """Extrai o primeiro CEP válido encontrado no texto (formato 00000-000 ou 00000000)."""
    match = re.search(r"\b(\d{5})-?(\d{3})\b", texto)
    if match:
        return match.group(1) + match.group(2)
    return None


async def consultar_cep(cep: str) -> Dict[str, Any]:
    """Consulta o ViaCEP e retorna os dados do endereço ou uma mensagem de erro."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(VIACEP_URL.format(cep=cep))
            if resp.status_code == 200:
                data = resp.json()
                if data.get("erro"):
                    return {"erro": True, "mensagem": f"CEP {cep} não encontrado na base dos Correios."}
                return {
                    "erro": False,
                    "cep": data.get("cep", cep),
                    "logradouro": data.get("logradouro", ""),
                    "bairro": data.get("bairro", ""),
                    "cidade": data.get("localidade", ""),
                    "estado": data.get("uf", ""),
                    "ibge": data.get("ibge", ""),
                }
            return {"erro": True, "mensagem": "Serviço de CEP indisponível no momento."}
    except Exception:
        return {"erro": True, "mensagem": "Serviço de CEP indisponível no momento."}


def formatar_dados_cep(dados: Dict[str, Any]) -> str:
    """Formata o resultado da consulta de CEP como texto legível."""
    if dados.get("erro"):
        return dados.get("mensagem", "Erro ao consultar CEP.")
    return (
        f"CEP: {dados['cep']}\n"
        f"Logradouro: {dados['logradouro'] or 'Não informado'}\n"
        f"Bairro: {dados['bairro'] or 'Não informado'}\n"
        f"Cidade: {dados['cidade']} / {dados['estado']}"
    )


async def processar_turno(payload: IncomingMessage) -> dict:
    """Processa o turno (Gemini + ViaCEP) e devolve o dict de resultado no formato Vonix."""
    # Extrai o texto da mensagem do campo text ou da última mensagem da lista
    user_message = payload.text
    if not user_message and payload.messages:
        last_msg = payload.messages[-1]
        user_message = last_msg.get("text", "")

    if not user_message:
        user_message = "Olá"

    # Tenta extrair e consultar o CEP direto no texto
    cep_encontrado = extrair_cep(user_message)
    dados_cep = ""
    if cep_encontrado:
        resultado = await consultar_cep(cep_encontrado)
        dados_cep = formatar_dados_cep(resultado)
    else:
        dados_cep = "Nenhum CEP foi identificado na mensagem do cliente ainda."

    system_instruction = """
    Você é o assistente especializado em consulta de CEP da loja 'Fake Atendimento E-commerce'.
    Seu único objetivo é ajudar o cliente a verificar se um CEP é válido, confirmar o endereço
    correspondente e informar se realizamos entregas naquela região.

    Regras de Comportamento:
    - Seja direto e objetivo. Sua especialidade é CEP e endereço.
    - Se o cliente informar um CEP, confirme os dados de endereço retornados pela consulta.
    - Se o CEP não for encontrado, peça gentilmente que o cliente verifique e informe novamente.
      Se persistir, use a ferramenta 'cep nao localizado' no campo exit.
    - Se o cliente não informou nenhum CEP ainda, solicite que informe o CEP para consulta.
    - Após confirmar o endereço com sucesso, informe que realizamos entregas normalmente na região
      e use a ferramenta 'Apos informar cep' no campo exit para finalizar o atendimento.
    - Marque tags relevantes: 'consulta_cep', 'endereco_confirmado', 'cep_invalido', etc.
    - Se precisar continuar conversando, mantenha 'exit' como null.

    Você DEVE retornar estritamente um JSON estruturado conforme o formato pedido.
    """

    history_text = (
        "\n".join([
            f"{h.get('role', 'contact')}: {h.get('text') or h.get('content', '')}"
            for h in payload.history
        ])
        if payload.history
        else "Sem histórico anterior."
    )

    tools_text = "\n".join([
        f"- id: {t.get('id')} | name: {t.get('name')} | type: {t.get('type')} | description: {t.get('description', '')}"
        for t in payload.tools
    ]) if payload.tools else "Nenhuma ferramenta disponível."

    user_prompt = f"""
    Dados do Contato: Nome: {payload.contact.name if payload.contact else 'Desconhecido'}, Telefone: {payload.contact.phone if payload.contact else 'Desconhecido'}
    Variáveis Atuais: {payload.variables}
    Ferramentas Disponíveis (use o 'id' da ferramenta no campo exit.tool):
    {tools_text}

    Resultado da Consulta de CEP:
    {dados_cep}

    Histórico Recente:
    {history_text}

    Nova Mensagem do Contato: {user_message}
    """

    # Orçamento de tempo: no fluxo síncrono o endpoint deve responder em até 30s.
    # Reservamos 5s de margem. No fluxo assíncrono (callback) o limite é maior,
    # mas o orçamento ainda evita loops longos desnecessários.
    TIME_BUDGET = 25
    start_time = time.monotonic()

    models_to_try = ['gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']
    max_retries = 3
    response = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(max_retries):
            # Verifica se ainda há tempo dentro do orçamento
            elapsed = time.monotonic() - start_time
            if elapsed >= TIME_BUDGET:
                print(f"[callback-agent][timeout] orçamento de {TIME_BUDGET}s atingido, abortando retries")
                break
            try:
                print(f"[callback-agent][gemini] calling {model_name}, attempt {attempt + 1}/{max_retries}")
                # Roda a chamada síncrona do Gemini numa thread separada para
                # não bloquear o event loop do asyncio.
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=GeminiAgentResponse,
                        temperature=0.3,
                    ),
                )
                break
            except Exception as api_err:
                last_error = api_err
                error_str = str(api_err)
                is_retryable = "503" in error_str or "UNAVAILABLE" in error_str or "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                if is_retryable and attempt < max_retries - 1:
                    delay = min((2 ** attempt) * 2, TIME_BUDGET - elapsed)
                    if delay <= 0:
                        break
                    print(f"[callback-agent][retry] {model_name} attempt {attempt + 1} failed, waiting {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    print(f"[callback-agent][fallback] {model_name} failed: {error_str[:100]}")
                    break
        if response:
            break

    if not response:
        error_str = str(last_error) if last_error else "erro desconhecido"
        is_429 = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
        is_503 = "503" in error_str or "UNAVAILABLE" in error_str
        if is_429:
            raise HTTPException(
                status_code=429,
                detail=str(last_error),
                headers={"Retry-After": "60"},
            )
        if is_503:
            raise HTTPException(
                status_code=503,
                detail=str(last_error),
                headers={"Retry-After": "1"},
            )
        raise HTTPException(status_code=500, detail=str(last_error))

    result_data = json.loads(response.text)

    # Constrói variables de retorno mapeando exit.note -> variável "note" se solicitado.
    # Montado em Python (fora do schema do Gemini) — somente chaves de return_variables são gravadas.
    if payload.return_variables:
        variables_retorno = {}
        exit_data = result_data.get("exit") or {}
        for var_name in payload.return_variables:
            if var_name == "note" and exit_data.get("note"):
                variables_retorno[var_name] = exit_data["note"]
        if variables_retorno:
            result_data["variables"] = variables_retorno

    # Converte messages de List[str] para o formato Vonix: {type, text}
    # Filtra mensagens vazias para evitar erro "text.body is required" no WhatsApp.
    # Limite de 10 itens por contrato da API de callback (além disso a resposta é inválida).
    if "messages" in result_data and isinstance(result_data["messages"], list):
        result_data["messages"] = [
            {"type": "TEXT", "text": msg}
            for msg in result_data["messages"]
            if msg and msg.strip()
        ][:10]

    return result_data


async def entregar_callback(callback_url: str, result: dict) -> None:
    """POSTa o resultado para a callback_url (fluxo 202).

    Regras derivadas da documentação da API de callback:
    - Sem header de autorização: a própria URL é a credencial por turno.
    - Não logar nem persistir a URL depois do uso.
    - Vale uma vez só: segunda chamada devolve 409 (already_resolved).
    - 404 (token desconhecido) e 409 (turno encerrado): NÃO retentar.
    - 400/413 (corpo inválido/maior que 1MB): NÃO retentar (não se resolve repetindo).
    - 5xx ou erro de rede: no máximo UMA nova tentativa com backoff.
    """
    tentativas_max = 2  # 1 envio + 1 retry apenas em 5xx/erro de rede
    for tentativa in range(tentativas_max):
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                # POST sem header de autorização; a URL usada exatamente como recebida.
                resp = await http.post(callback_url, json=result)
        except Exception as e:
            # Log sem expor a URL (credencial do turno).
            print(f"[callback] erro de rede na entrega: {type(e).__name__}")
            if tentativa < tentativas_max - 1:
                await asyncio.sleep(2)
                continue
            return

        # 202: corpo recebido e aplicado depois. 200: aplicado de imediato. Sucesso.
        if resp.status_code in (200, 202):
            return

        # Turno já encerrado (already_resolved/expired/chat_closed) ou token desconhecido:
        # repetir não adianta — o turno não reabre.
        if resp.status_code in (404, 409):
            print(f"[callback] turno indisponível (status {resp.status_code}); não será reenviado")
            return

        # Falha interna do servidor: uma única retentativa, depois desiste.
        if 500 <= resp.status_code < 600:
            print(f"[callback] erro interno {resp.status_code} na tentativa {tentativa + 1}")
            if tentativa < tentativas_max - 1:
                await asyncio.sleep(2)
                continue
            return

        # 400/413 e demais 4xx: corpo inválido ou muito grande; não retratável.
        print(f"[callback] resposta não retratável: {resp.status_code}")
        return


async def processar_e_entregar(payload: IncomingMessage) -> None:
    """Pipeline executado em background após o 202:
    processa o turno e entrega o resultado via callback_url.
    """
    try:
        result = await processar_turno(payload)
        if payload.callback_url:
            await entregar_callback(payload.callback_url, result)
    except Exception:
        # Em caso de falha no processamento não há corpo válido para enviar.
        # O turno expira sozinho (callback_deadline) e o chat segue pela saída
        # Fallback do bloco. Não relançamos: a resposta 202 já foi dada.
        traceback.print_exc()


@app.post(
    "/webhook/callback",
    response_model=AgentResponse,
    tags=["Webhook"],
    summary="Webhook do Agente CEP (fluxo síncrono 200 ou assíncrono 202 + callback)",
    description="""
Recebe o turno enviado pela plataforma (Vonix) e processa via ViaCEP + Gemini.

## Decisão de fluxo baseada em `callback_url`

- **Com `callback_url`** → responde `202 Accepted` imediatamente e processa em background.
  O resultado é entregue via `POST` para a `callback_url` (uso único, sem header de auth).
- **Sem `callback_url`** → processa de forma síncrona e retorna o resultado no corpo (`200 OK`).

## Regras de ouro da `callback_url`

- Credencial única de uso único por turno (segunda chamada → **409**).
- Sem header de autorização — a própria URL autoriza.
- Não persistir em logs — use, entregue, descarte.
- Use exatamente como recebida (o token final não é montado pelo agente).

## Resposta assíncrona (POST na callback_url)

O corpo enviado à `callback_url` segue o mesmo `AgentResponse` (messages/tags/variables/exit).
Em caso de erro não retratável (404/409/400/413), o turno segue encerrado e o chat cai na saída
**Fallback** do bloco.
""",
    responses={
        200: {
            "description": "Resposta síncrona (fluxo sem `callback_url`): payload processado retornado no corpo.",
            "model": AgentResponse,
        },
        202: {
            "description": "Turno aceito para processamento em background (fluxo com `callback_url`). "
            "O resultado será entregue via POST à `callback_url`. O corpo da resposta é vazio.",
        },
        400: {
            "description": "Corpo JSON inválido ou malformado. **Não retentar** — corrija a estrutura do payload enviado.",
            "model": ErrorResponse,
        },
        413: {
            "description": "Corpo da requisição acima de 1 MB. **Não retentar** — reduza o array de mensagens/histórico.",
            "model": ErrorResponse,
        },
        500: {
            "description": "Falha interna (ex.: Gemini indisponível após fallback). No fluxo de callback, o agente "
            "faz no máximo 1 retry com backoff no envio à callback_url em caso de 5xx/erro de rede. "
            "Atenção: um novo envio **não reabre** o turno (ele segue encerrado).",
            "model": ErrorResponse,
        },
    },
)
async def handle_callback_turn(payload: IncomingMessage, background_tasks: BackgroundTasks):
    """Webhook do agente CEP com fluxo de callback assíncrono.

    - Com callback_url: responde 202 imediatamente e processa em background,
      entregando o resultado via POST para a callback_url (uso único, sem header de auth).
    - Sem callback_url: mantém o fluxo síncrono (200) para testes e compatibilidade.
    """
    if payload.callback_url:
        background_tasks.add_task(processar_e_entregar, payload)
        return Response(status_code=202)

    # Fluxo síncrono (sem callback_url)
    try:
        return await processar_turno(payload)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Verifica se o serviço está online. Útil para probes de liveness no Render.",
)
def health_check():
    return {"status": "online", "service": "Agente CEP - Callback"}
