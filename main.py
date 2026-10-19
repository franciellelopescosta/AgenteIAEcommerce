import os
import traceback
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


app = FastAPI(title="Fake Atendimento E-commerce Agent")

client = genai.Client()


class Contact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    user_id: Optional[str] = None


class IncomingMessage(BaseModel):
    contact: Optional[Contact] = None
    history: Optional[List[Dict[str, Any]]] = []
    variables: Optional[Dict[str, Any]] = {}
    message: Optional[str] = Field(default="", description="Mensagem de texto direta")
    text: Optional[str] = Field(default="", description="Texto enviado pela Vonix")
    messages: Optional[List[Dict[str, Any]]] = Field(default=[], description="Lista de mensagens da Vonix")
    tools: Optional[List[Dict[str, Any]]] = Field(default=[], description="Ferramentas disponíveis")


class AgentResponse(BaseModel):
    messages: List[str] = Field(description="Lista de mensagens de texto para enviar ao contato")
    tags_to_add: Optional[List[str]] = Field(default=[], description="Tags para marcar no atendimento")
    action: Optional[str] = Field(default=None, description="Ação de saída: 'next_block', 'transfer_queue', 'transfer_flow', 'close', ou null para continuar conversando")
    action_target: Optional[str] = Field(default=None, description="Nome ou ID da fila/fluxo destino, caso haja transferência")
    ai_summary: Optional[str] = Field(default=None, description="Resumo obrigatório se for transferir ou encerrar")


@app.post("/webhook/ia", response_model=AgentResponse)
async def handle_agent_turn(payload: IncomingMessage):
    try:
        # Extrai o texto da mensagem independentemente de vir em 'message', 'text' ou 'messages'
        user_message = payload.message or payload.text
        if not user_message and payload.messages:
            last_msg = payload.messages[-1]
            user_message = last_msg.get("text", "")

        if not user_message:
            user_message = "Olá"

        system_instruction = """
        Você é o assistente virtual de atendimento da 'Fake Atendimento E-commerce', uma loja virtual genérica.
        Seu objetivo é ajudar os clientes com dúvidas sobre produtos, prazos de entrega, status de pedidos, trocas e devoluções.

        Regras de Comportamento:
        - Seja educado, prestativo e direto.
        - Tente identificar o número do pedido ou o problema principal do cliente.
        - Salve informações úteis nas variáveis (como 'numero_pedido' ou 'tipo_duvida').
        - Marque tags relevantes no atendimento (ex: 'duvida_frete', 'troca', 'status_pedido').
        - Se o problema for resolvido ou se enquadrar nas ferramentas de fila/transferência disponíveis, escolha a ação adequada ('transfer_queue', etc.) e preencha o 'ai_summary' com um resumo do caso.
        - Se precisar continuar conversando, mantenha 'action' como null.

        Você DEVE retornar estritamente um JSON estruturado conforme o formato pedido.
        """

        history_text = (
            "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in payload.history])
            if payload.history
            else "Sem histórico anterior."
        )

        user_prompt = f"""
        Dados do Contato: Nome: {payload.contact.name if payload.contact else 'Desconhecido'}, Telefone: {payload.contact.phone if payload.contact else 'Desconhecido'}
        Variáveis Atuais: {payload.variables}
        Ferramentas Disponíveis: {payload.tools}
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
                            response_schema=AgentResponse,
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
                break  # Tem resposta, não precisa tentar o próximo modelo

        if not response:
            raise HTTPException(status_code=500, detail=str(last_error))

        result_data = json.loads(response.text)
        return result_data

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def health_check():
    return {"status": "online", "service": "Fake Atendimento E-commerce Agent"}
