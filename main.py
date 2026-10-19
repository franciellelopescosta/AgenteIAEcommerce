import os
import traceback
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


app = FastAPI(title="Fake Atendimento E-commerce Agent")

client = genai.Client()

RASTREIO_API_URL = "https://api-frases-teste.onrender.com/retornorastreios"


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
    text: Optional[str] = Field(default="", description="Textos do turno unidos por quebra de linha")
    messages: Optional[List[Dict[str, Any]]] = Field(default=[], description="Mensagens novas do contato neste turno")
    history: Optional[List[Dict[str, Any]]] = Field(default=[], description="Histórico de mensagens anteriores")
    variables: Optional[Dict[str, Any]] = {}
    return_variables: Optional[List[str]] = Field(default=[], description="Variáveis que o agente pode gravar")
    tools: Optional[List[Dict[str, Any]]] = Field(default=[], description="Saídas permitidas neste turno")


# Schema interno usado pelo Gemini para gerar a resposta estruturada
class GeminiExit(BaseModel):
    tool: Optional[str] = Field(default=None, description="ID da ferramenta de saída (tool.id)")
    note: Optional[str] = Field(default=None, description="Resumo do caso para transferência ou encerramento")


class GeminiAgentResponse(BaseModel):
    messages: List[str] = Field(description="Lista de textos para enviar ao contato")
    tags: Optional[List[str]] = Field(default=[], description="Tags para marcar no atendimento")
    exit: Optional[GeminiExit] = Field(default=None, description="Saída do bloco. Null para continuar conversando")


async def buscar_rastreios() -> str:
    """Consulta a API de rastreios e retorna os dados formatados como texto."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(RASTREIO_API_URL)
            if resp.status_code == 200:
                data = resp.json()
                rastreios = data.get("rastreios", [])
                if not rastreios:
                    return "Nenhum rastreio disponível no momento."
                linhas = []
                for r in rastreios:
                    linhas.append(
                        f"- Código: {r['numero_rastreio']} | Produto: {r['produto']} | "
                        f"Status: {r['status']} | Previsão de entrega: {r['previsao_entrega']}"
                    )
                return "\n".join(linhas)
            return "Serviço de rastreio indisponível no momento."
    except Exception:
        return "Serviço de rastreio indisponível no momento."


@app.post("/webhook/ia")
async def handle_agent_turn(payload: IncomingMessage):
    try:
        # Extrai o texto da mensagem do campo text ou da última mensagem da lista
        user_message = payload.text
        if not user_message and payload.messages:
            last_msg = payload.messages[-1]
            user_message = last_msg.get("text", "")

        if not user_message:
            user_message = "Olá"

        # Consulta os rastreios para incluir no contexto do agente
        rastreios_info = await buscar_rastreios()

        system_instruction = """
        Você é o assistente virtual de atendimento da 'Fake Atendimento E-commerce', uma loja virtual genérica.
        Seu objetivo é ajudar os clientes com dúvidas sobre produtos, prazos de entrega, status de pedidos, trocas e devoluções.

        Regras de Comportamento:
        - Seja educado, prestativo e direto.
        - Quando o cliente perguntar sobre rastreio ou entrega, consulte os dados de rastreio disponíveis no contexto e informe o status do pedido correspondente. Após informar, pergunte se o cliente precisa de mais alguma coisa ou se pode encerrar o atendimento. Se o código não estiver na lista, informe ao cliente que não foi possível localizar o pedido e que ele será direcionado a um agente humano para ajudá-lo, depois use a ferramenta 'Codigo de rastreio' no campo exit.
        - Marque tags relevantes no atendimento (ex: 'duvida_frete', 'troca', 'status_pedido').
        - Se a situação se enquadrar em uma das ferramentas disponíveis, preencha o campo 'exit' com o 'id' da ferramenta no campo 'tool' e um resumo no campo 'note'.
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
        Dados de Rastreio Disponíveis:
        {rastreios_info}
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
                    print(f"[gemini] calling {model_name}, attempt {attempt + 1}/{max_retries}")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            response_mime_type="application/json",
                            response_schema=GeminiAgentResponse,
                            temperature=0.4,
                        ),
                    )
                    break  # Sucesso, sai do loop de tentativas
                except Exception as api_err:
                    last_error = api_err
                    error_str = str(api_err)
                    is_retryable = "503" in error_str or "UNAVAILABLE" in error_str
                    if is_retryable and attempt < max_retries - 1:
                        delay = (2 ** attempt) * 2  # 2s, 4s
                        print(f"[retry] {model_name} attempt {attempt + 1} failed with 503, waiting {delay}s...")
                        time.sleep(delay)
                    else:
                        print(f"[fallback] {model_name} failed: {error_str[:100]}")
                        break  # Tenta o próximo modelo
            if response:
                break

        if not response:
            raise HTTPException(status_code=500, detail=str(last_error))

        result_data = json.loads(response.text)

        # Converte messages de List[str] para o formato esperado pela Vonix: {type, text}
        # Filtra mensagens vazias para evitar erro "text.body is required" no WhatsApp
        if "messages" in result_data and isinstance(result_data["messages"], list):
            result_data["messages"] = [
                {"type": "TEXT", "text": msg}
                for msg in result_data["messages"]
                if msg and msg.strip()
            ]

        return result_data

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def health_check():
    return {"status": "online", "service": "Fake Atendimento E-commerce Agent"}
