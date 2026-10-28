# Agente IA E-commerce

Conjunto de microsserviços de atendimento conversacional integrados à plataforma **Vonix**, usando **Google Gemini** para geração de respostas em linguagem natural.

---

## Visão Geral

O projeto é composto por três agentes independentes, cada um com uma responsabilidade específica:

| Agente | Arquivo | Endpoint | Responsabilidade |
|---|---|---|---|
| Atendimento Geral | `main.py` | `POST /webhook/ia` | Triagem geral: catálogo, rastreio de pedidos, transferências |
| Agente CEP (síncrono) | `cep_agent.py` | `POST /webhook/cep` | Consulta de CEP via ViaCEP |
| Agente CEP (callback) | `callback_agent.py` | `POST /webhook/callback` | Consulta de CEP com suporte a fluxo assíncrono (202 + callback) |

---

## Agentes

### 1. Atendimento Geral (`main.py`)

Agente principal de triagem. Atende dúvidas gerais dos clientes e direciona para os fluxos corretos.

**Funcionalidades:**
- Envia link do catálogo de produtos
- Consulta status de rastreio via API externa
- Transfere para agente humano quando o código não é encontrado
- Direciona para pesquisa de satisfação após rastreio bem-sucedido
- Transfere para o Agente CEP quando o cliente pergunta sobre entrega por localidade
- Marca tags relevantes no atendimento

**Autenticação:** `Authorization: <VONIX_TOKEN>` (header obrigatório em turnos reais; turnos de teste `is_test: true` são isentos)

**Variáveis de ambiente:**
```
VONIX_TOKEN      Token de autenticação configurado no bloco da Vonix
GOOGLE_API_KEY   Chave da API do Google Gemini
```

---

### 2. Agente CEP Síncrono (`cep_agent.py`)

Agente especializado em validação de endereços. Extrai o CEP da mensagem do cliente, consulta o **ViaCEP** e retorna os dados do endereço.

**Funcionalidades:**
- Extrai automaticamente o CEP do texto (formatos `00000-000` ou `00000000`)
- Confirma logradouro, bairro, cidade e estado
- Orienta o cliente quando o CEP não é encontrado
- Transfere para atendimento geral quando a dúvida está fora do escopo

**Endpoint:** `POST /webhook/cep`  
**Autenticação:** Nenhuma (sem validação de token)

---

### 3. Agente CEP com Callback (`callback_agent.py`)

Versão avançada do Agente CEP com suporte a **fluxo assíncrono**. Recomendado para integrações em produção onde a plataforma suporta callback.

**Funcionalidades:**
- Tudo que o Agente CEP síncrono faz
- Fluxo assíncrono: responde `202 Accepted` imediatamente e processa em background
- Entrega o resultado via `POST` na `callback_url` fornecida pela plataforma
- Retry automático em caso de erro de rede ou 5xx na entrega do callback
- Documentação Swagger disponível em `/docs` e `/redoc`

**Endpoint:** `POST /webhook/callback`  
**Autenticação:** Nenhuma (a `callback_url` é a credencial por turno)

#### Fluxo de decisão

```
Payload recebido
       │
       ├── com callback_url ──► 202 Accepted (imediato)
       │                              │
       │                        Background task
       │                              │
       │                        ViaCEP + Gemini
       │                              │
       │                        POST callback_url
       │
       └── sem callback_url ──► 200 OK (síncrono)
```

#### Regras da `callback_url`
- Credencial única por turno — segunda chamada retorna `409`
- Sem header de autorização — a URL em si autoriza o envio
- Não deve ser persistida em logs
- Use exatamente como recebida

---

## Tecnologias

- **[FastAPI](https://fastapi.tiangolo.com/)** — framework web
- **[Google Gemini](https://ai.google.dev/)** — geração de respostas com structured output
- **[ViaCEP](https://viacep.com.br/)** — consulta de endereços por CEP
- **[httpx](https://www.python-httpx.org/)** — cliente HTTP assíncrono
- **[Pydantic](https://docs.pydantic.dev/)** — validação de dados
- **[Uvicorn](https://www.uvicorn.org/)** — servidor ASGI

---

## Instalação

```bash
# Clone o repositório
git clone https://github.com/franciellelopescosta/AgenteIAEcommerce.git
cd AgenteIAEcommerce

# Crie e ative o ambiente virtual
python -m venv venv
source venv/bin/activate

# Instale as dependências
pip install -r requirements.txt
```

---

## Configuração

Crie um arquivo `.env` ou configure as variáveis de ambiente diretamente:

```env
GOOGLE_API_KEY=sua_chave_aqui
VONIX_TOKEN=seu_token_aqui
```

---

## Execução local

Cada agente roda em um processo separado. Escolha o que deseja executar:

```bash
# Agente de Atendimento Geral
uvicorn main:app --reload --port 8000

# Agente CEP Síncrono
uvicorn cep_agent:app --reload --port 8001

# Agente CEP com Callback
uvicorn callback_agent:app --reload --port 8002
```

---

## Deploy (Render)

O projeto usa `Procfile` para definir o processo principal:

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Para subir os outros agentes no Render, crie serviços separados usando os `Procfile` alternativos:

| Procfile | Agente |
|---|---|
| `Procfile` | Atendimento Geral |
| `Procfile.cep` | Agente CEP Síncrono |
| `Procfile.callback` | Agente CEP com Callback |

**Variáveis de ambiente no Render:**

| Variável | Descrição |
|---|---|
| `GOOGLE_API_KEY` | Chave da API do Google Gemini |
| `VONIX_TOKEN` | Token de autenticação do bloco Vonix (apenas para o Agente Geral) |

---

## Formato de resposta (Vonix)

Todos os agentes retornam o mesmo formato esperado pela Vonix:

```json
{
  "messages": [
    { "type": "TEXT", "text": "Mensagem para o cliente" }
  ],
  "tags": ["tag_relevante"],
  "variables": { "note": "Resumo do caso" },
  "exit": {
    "tool": "id-da-ferramenta",
    "note": "Resumo para transferência"
  }
}
```

- `exit: null` → cliente permanece no mesmo bloco conversando
- `exit.tool` → deve corresponder ao `id` de uma ferramenta configurada no bloco da Vonix
