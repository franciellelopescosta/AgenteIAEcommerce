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
import re


app = FastAPI(title="Agente de CEP - E-commerce")

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


class GeminiExit(BaseModel):
    tool: Optional[str] = Field(default=None, description="ID da ferramenta de saída")
    note: Optional[str] = Field(default=None, description="Resumo para transferência ou encerramento")


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


@app.post("/webhook/cep")
async def handle_cep_turn(payload: IncomingMessage):
    try:
        # Extrai o texto da mensagem
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
        - Se o cliente não informou nenhum CEP ainda, solicite que informe o CEP para consulta.
        - Após confirmar o endereço, pergunte se o cliente precisa de mais alguma coisa.
        - Se o cliente tiver dúvidas fora do escopo de CEP/endereço, use a ferramenta de transferência
          disponível para encaminhar ao atendimento geral.
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
                    print(f"[cep-agent][gemini] calling {model_name}, attempt {attempt + 1}/{max_retries}")
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
                        print(f"[cep-agent][retry] {model_name} attempt {attempt + 1} failed, waiting {delay}s...")
                        time.sleep(delay)
                    else:
                        print(f"[cep-agent][fallback] {model_name} failed: {error_str[:100]}")
                        break
            if response:
                break

        if not response:
            raise HTTPException(status_code=500, detail=str(last_error))

        result_data = json.loads(response.text)

        # Converte messages para o formato Vonix: {type, text}
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
    return {"status": "online", "service": "Agente de CEP - E-commerce"}
