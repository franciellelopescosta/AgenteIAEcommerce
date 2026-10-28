import os
import secrets
import traceback
import asyncio
import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


app = FastAPI(title="Fake Atendimento E-commerce Agent")

client = genai.Client()

RASTREIO_API_URL = "https://api-frases-teste.onrender.com/retornorastreios"

# Token de autorização configurado no bloco da Vonix (env var VONIX_TOKEN no Render).
# A Vonix envia no header: Authorization: Bearer <VONIX_TOKEN>
VONIX_TOKEN = os.environ.get("VONIX_TOKEN", "")


class Contact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    user_id: Optional[str] = None


class IncomingMessage(BaseModel):
    # Todos os campos abaixo são obrigatórios pelo contrato da API Vonix.
    turn_id: str = Field(description="Id do turno (igual na retentativa)")
    chat_id: int = Field(description="Id do atendimento (use como id de sessão se o agente guarda memória)")
    node_id: str = Field(description="Id do bloco no fluxo")
    channel: str = Field(description="Canal do atendimento")
    is_test: bool = Field(description="True quando o turno vem do testador de fluxo ou do botão Testar requisição")
    contact: Contact = Field(description="Dados do contato (qualquer campo pode vir vazio)")
    text: str = Field(description="Textos e legendas de messages unidos por quebra de linha")
    messages: List[Dict[str, Any]] = Field(description="Mensagens novas do contato neste turno, na ordem de chegada")
    history: List[Dict[str, Any]] = Field(description="Histórico de mensagens anteriores do atendimento atual")
    variables: Dict[str, Any] = Field(description="Variáveis de contexto do fluxo")
    return_variables: List[str] = Field(description="Nomes que o agente pode gravar em variables na resposta")
    tools: List[Dict[str, Any]] = Field(description="Saídas permitidas neste turno")


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
        async with httpx.AsyncClient(timeout=5) as http:
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
async def handle_agent_turn(
    payload: IncomingMessage,
    authorization: str = Header(default="", description="Token de autorização (Bearer)"),
    x_vonix_turn_id: str = Header(..., description="Igual a turn_id. Repete-se na retentativa"),
    x_vonix_attempt: int = Header(..., description="1 na primeira tentativa, 2 na retentativa"),
):
    # Validação do Bearer token configurado no bloco da Vonix.
    # Turnos de teste (is_test=True) são isentos de autenticação — o testador de fluxo
    # e o botão "Testar requisição" da Vonix não enviam o header Authorization.
    # Token ausente/inválido em turnos reais -> 401 (Vonix encaminha para saída Fallback).
    if not payload.is_test:
        # A Vonix pode enviar o token como "Bearer <token>" ou apenas "<token>".
        # Normaliza removendo o prefixo antes de comparar.
        received_token = authorization.removeprefix("Bearer ").strip()
        print(f"[auth] token recebido (completo): {received_token!r}")
        print(f"[auth] token esperado (completo): {VONIX_TOKEN!r}")
        print(f"[auth] match: {received_token == VONIX_TOKEN}")
        if not VONIX_TOKEN or received_token != VONIX_TOKEN:
            print(f"[auth] FALHOU - lens: recebido={len(received_token)} esperado={len(VONIX_TOKEN)}")
            raise HTTPException(status_code=401, detail="Token inválido")

    # Validação do X-Vonix-Attempt (deve ser 1 ou 2 conforme o contrato).
    if x_vonix_attempt not in (1, 2):
        raise HTTPException(status_code=422, detail="X-Vonix-Attempt deve ser 1 ou 2")

    # Loga turnos de teste (is_test=true vem do testador de fluxo ou do botão Testar requisição).
    # Evite efeitos reais nesse caso — aqui apenas logamos, pois as chamadas externas são
    # apenas leitura (GET na API de rastreio e geração de texto no Gemini).
    if payload.is_test:
        print(f"[test] turno de teste recebido (turn_id={payload.turn_id})")

    # Identifica retentativa (X-Vonix-Attempt=2) para fins de observabilidade.
    if x_vonix_attempt == 2:
        print(f"[retry] retentativa recebida para o turno {x_vonix_turn_id}")

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
        - Quando o cliente solicitar o catálogo de produtos, envie a mensagem: "🛍️ Aproveite nossas ofertas, nossos produtos são incríveis! Acesse nosso catálogo completo: https://catalogoa2.onrender.com/" e em seguida pergunte: "Posso te ajudar com mais alguma coisa?". Se o cliente responder que não precisa de mais nada ou demonstrar satisfação, agradeça o contato, despeça-se cordialmente.
        - Quando o cliente demonstrar interesse em rastreio ou entrega mas não informar nenhum código, peça gentilmente o número do pedido ou código de rastreio antes de qualquer consulta.
        - Quando o cliente informar um código de rastreio, consulte os dados de rastreio disponíveis no contexto e informe o status do pedido correspondente. Após informar, pergunte se o cliente precisa de mais alguma coisa.
        - Se o código não estiver na lista, informe ao cliente que não foi possível localizar o pedido e que ele será direcionado a um agente humano, depois use a ferramenta 'Codigo de rastreio' no campo exit.
        - Após informar o rastreio com sucesso e o cliente responder que não precisa de mais nada, informe que ele será direcionado para uma rápida pesquisa de satisfação e use a ferramenta 'Pesquisa' no campo exit.
        - Quando o cliente mencionar CEP, quiser verificar se entregamos em um endereço, ou perguntar sobre frete por localidade, informe que irá transferi-lo para o nosso assistente especializado em consulta de endereço, e use a ferramenta com name 'Agente CEP' ou similar no campo exit. Marque a tag 'consulta_cep'.
        - Marque tags relevantes no atendimento (ex: 'duvida_frete', 'troca', 'status_pedido', 'consulta_cep').
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

        # Orçamento de tempo: o endpoint deve responder em até 30s.
        # Reservamos 5s de margem para a serialização/envio da resposta HTTP.
        TIME_BUDGET = 25
        start_time = time.monotonic()

        models_to_try = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-1.5-flash-8b']
        max_retries = 3
        response = None
        last_error = None

        for model_name in models_to_try:
            for attempt in range(max_retries):
                # Verifica se ainda há tempo dentro do orçamento de 30s
                elapsed = time.monotonic() - start_time
                if elapsed >= TIME_BUDGET:
                    print(f"[timeout] orçamento de {TIME_BUDGET}s atingido, abortando retries")
                    break
                try:
                    print(f"[gemini] calling {model_name}, attempt {attempt + 1}/{max_retries}")
                    # Roda a chamada síncrona do Gemini numa thread separada para
                    # não bloquear o event loop do asyncio enquanto espera a resposta.
                    response = await asyncio.to_thread(
                        client.models.generate_content,
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
                    is_retryable = "503" in error_str or "UNAVAILABLE" in error_str or "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                    if is_retryable and attempt < max_retries - 1:
                        delay = min((2 ** attempt) * 2, TIME_BUDGET - elapsed)
                        if delay <= 0:
                            break
                        print(f"[retry] {model_name} attempt {attempt + 1} failed, waiting {delay}s...")
                        await asyncio.sleep(delay)
                    else:
                        print(f"[fallback] {model_name} failed: {error_str[:100]}")
                        break  # Tenta o próximo modelo
            if response:
                break

        if not response:
            error_str = str(last_error) if last_error else "erro desconhecido"
            is_429 = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            is_503 = "503" in error_str or "UNAVAILABLE" in error_str
            if is_429:
                # 429 -> a Vonix retenta respeitando Retry-After (se couber no tempo do bloco)
                raise HTTPException(
                    status_code=429,
                    detail=str(last_error),
                    headers={"Retry-After": "60"},
                )
            if is_503:
                # 503 -> a Vonix retenta após 1s (se couber no tempo do bloco)
                raise HTTPException(
                    status_code=503,
                    detail=str(last_error),
                    headers={"Retry-After": "1"},
                )
            # 500/default -> Fallback sem retentativa
            raise HTTPException(status_code=500, detail=str(last_error))

        result_data = json.loads(response.text)

        # Constrói variables de retorno mapeando exit.note -> variável "note" se solicitado
        if payload.return_variables:
            variables_retorno = {}
            exit_data = result_data.get("exit") or {}
            for var_name in payload.return_variables:
                if var_name == "note" and exit_data.get("note"):
                    variables_retorno[var_name] = exit_data["note"]
            if variables_retorno:
                result_data["variables"] = variables_retorno

        # Converte messages de List[str] para o formato esperado pela Vonix: {type, text}
        # Filtra mensagens vazias para evitar erro "text.body is required" no WhatsApp
        # e limita a 10 itens (limite do contrato da API).
        if "messages" in result_data and isinstance(result_data["messages"], list):
            result_data["messages"] = [
                {"type": "TEXT", "text": msg}
                for msg in result_data["messages"]
                if msg and msg.strip()
            ][:10]

        return result_data

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def health_check():
    return {"status": "online", "service": "Fake Atendimento E-commerce Agent"}
