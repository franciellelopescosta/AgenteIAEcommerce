import traceback
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from google import genai
from google.genai import types
import json
import time


app = FastAPI(title="Agente E-commerce - Callback")

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


async def processar_turno(payload: IncomingMessage) -> dict:
    """Processa o turno (Gemini + rastreio) e devolve o dict de resultado no formato Vonix.
    É a mesma lógica que o fluxo síncrono do main.py, isolada para reuso no callback.
    """
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
                    print(f"[callback-agent][retry] {model_name} attempt {attempt + 1} failed with 503, waiting {delay}s...")
                    time.sleep(delay)
                else:
                    print(f"[callback-agent][fallback] {model_name} failed: {error_str[:100]}")
                    break  # Tenta o próximo modelo
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

        # Falha interna do servidor: uma única re tentativa, depois desiste.
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
    """Webhook do agente com fluxo de callback assíncrono.

    - Se o payload traz callback_url: responde 202 imediatamente e processa em
      background, entregando o resultado via POST para a callback_url (uso único).
    - Se não traz callback_url: mantém o fluxo síncrono (200) para testes e
      compatibilidade com plataformas que esperam resposta no corpo.
    """
    if payload.callback_url:
        # A URL é usada exatamente como recebida (o token final NÃO é montado por nós).
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
    return {"status": "online", "service": "Agente E-commerce - Callback"}
