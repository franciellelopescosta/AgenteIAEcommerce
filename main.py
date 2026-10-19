import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json

app = FastAPI(title="Fake Atendimento E-commerce Agent")

client = genai.Client()

class Contact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None

class IncomingMessage(BaseModel):
    contact: Optional[Contact] = None
    history: Optional[List[Dict[str, str]]] = []
    variables: Optional[Dict[str, Any]] = {}
    message: str

# Schema simplificado para o Gemini aceitar sem erros de additional_properties
class AgentResponse(BaseModel):
    messages: List[str] = Field(description="Lista de mensagens de texto para enviar ao contato")
    tags_to_add: Optional[List[str]] = Field(default=[], description="Tags para marcar no atendimento")
    action: Optional[str] = Field(default=None, description="Ação de saída: 'next_block', 'transfer_queue', 'transfer_flow', 'close', ou null para continuar conversando")
    action_target: Optional[str] = Field(default=None, description="Nome ou ID da fila/fluxo destino, caso haja transferência")
    ai_summary: Optional[str] = Field(default=None, description="Resumo obrigatório se for transferir ou encerrar")

@app.post("/webhook/ia", response_model=AgentResponse)
async def handle_agent_turn(payload: IncomingMessage):
    try:
        system_instruction = """
        Você é o assistente virtual de atendimento da 'Fake Atendimento E-commerce', uma loja virtual genérica.
        Seu objetivo é ajudar os clientes com dúvidas sobre produtos, prazos de entrega, status de pedidos, trocas e devoluções.
        
        Regras de Comportamento:
        - Seja educado, prestativo e direto.
        - Tente identificar o número do pedido ou o problema principal do cliente.
        - Marque tags relevantes no atendimento (ex: 'duvida_frete', 'troca', 'status_pedido').
        - Se o problema for resolvido ou o cliente quiser falar com um atendente humano, escolha a ação adequada para transferir para a fila de suporte humano ou encerrar o atendimento, preenchendo obrigatoriamente o 'ai_summary' com um resumo do caso.
        - Se precisar continuar conversando, mantenha 'action' como null.
        
        Você DEVE retornar estritamente um JSON estruturado conforme o formato pedido.
        """

        history_text = "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in payload.history]) if payload.history else "Sem histórico anterior."
        
        user_prompt = f"""
        Dados do Contato: Nome: {payload.contact.name if payload.contact else 'Desconhecido'}, Telefone: {payload.contact.phone if payload.contact else 'Desconhecido'}
        Variáveis Atuais: {payload.variables}
        Histórico Recente:
        {history_text}
        
        Nova Mensagem do Contato: {payload.message}
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=AgentResponse,
                temperature=0.4,
            ),
        )

        result_data = json.loads(response.text)
        return result_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "online", "service": "Fake Atendimento E-commerce Agent"}