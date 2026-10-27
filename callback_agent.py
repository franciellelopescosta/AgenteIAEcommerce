import traceback
import httpx
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


app = FastAPI(title="Agente CEP - Callback")

client = genai.Client()

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"


class Contact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    user_id: Optional[str] = None


class IncomingMessage(BaseModel):
    turn_id: Optional[str] = None
    chat_id: Optional[int] = None
    node_id: Optional[str] = None
    channel: Optional[str] = None
    is_test: Optional[bool] = False
    contact: Optional[Contact] = None
    text: Optional[str] = Field(default="", description="Texto do turno")
    messages: Optional[List[Dict[str, Any]]] = Field(default=[], description="Mensagens novas do contato")
    history: Optional[List[Dict[str, Any]]] = Field(default=[], description="Histórico de mensagens anteriores")
    variables: Optional[Dict[str, Any]] = {}
    return_variables: Optional[List[str]] = Field(default=[], description="Variáveis que o agente pode gravar")
    tools: Optional[List[Dict[str, Any]]] = Field(default=[], description="Saídas permitidas neste turno")
    callback_url: Optional[str] = Field(default=None, description="URL de callback para entregar o resultado de forma assíncrona (fluxo 202)")


# Schema interno usado pelo Gemini para gerar a resposta estruturada.
# IMPORTANTE: o campo "variables" NÃO pode estar neste schema — Dict[str, Any]
# é incompatível com o structured output do Gemini (INVALID_ARGUMENT).
# Ele é montado em Python, depois da resposta, a partir de return_variables.
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

    models_to_try = ['gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']
    max_retries = 3
    response = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(max_retries):
            try:
                print(f"[callback-agent][gemini] calling {model_name}, attempt {attempt + 1}/{max_retries}")
                response = client.models.generate_content(
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
                is_retryable = "503" in error_str or "UNAVAILABLE" in error_str
                if is_retryable and attempt < max_retries - 1:
                    delay = (2 ** attempt) * 2
                    print(f"[callback-agent][retry] {model_name} attempt {attempt + 1} failed with 503, waiting {delay}s...")
                    time.sleep(delay)
                else:
                    print(f"[callback-agent][fallback] {model_name} failed: {error_str[:100]}")
                    break
        if response:
            break

    if not response:
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
                time.sleep(2)
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
                time.sleep(2)
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


@app.post("/webhook/callback")
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


@app.get("/")
def health_check():
    return {"status": "online", "service": "Agente CEP - Callback"}
