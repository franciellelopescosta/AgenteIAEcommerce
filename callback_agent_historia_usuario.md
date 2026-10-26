# História de Usuário — Agente de Callback (`callback_agent.py`)

Documentação do fluxo assíncrono de callback (202 + POST para `callback_url`) implementado no `callback_agent.py`, modelado no agente principal (`main.py`).

---

## História de usuário

**Como** cliente da loja Fake Atendimento E-commerce,
**eu quero** perguntar o status do meu pedido pelo WhatsApp,
**para que** eu saiba quando meu produto vai chegar, sem precisar esperar na linha de atendimento humano.

### Cenário principal: cliente pede rastreio e o agente responde via callback

- **Dado** que o atendimento já está no bloco configurado para o fluxo de callback
- **Quando** o cliente manda "Quero saber o status do pedido, código BR123456"
- **Então** a plataforma (Vonix) monta um turno e envia para o agente
- **E** o agente responde `202` imediatamente (recebido, vou processar)
- **E** processa em background (consulta rastreios + Gemini)
- **E** entrega o resultado via `POST` para a `callback_url`
- **E** o cliente recebe a mensagem no WhatsApp com o status do pedido

---

## O passo a passo com payloads reais

### 1. A plataforma posta o turno no agente — `POST /webhook/callback`

```json
{
  "turn_id": "turn-8842",
  "chat_id": 1023,
  "contact": { "name": "Maria", "phone": "5511999990000" },
  "text": "Quero saber o status do pedido, código BR123456",
  "messages": [{ "type": "TEXT", "text": "Quero saber o status do pedido, código BR123456" }],
  "history": [],
  "variables": {},
  "return_variables": ["note"],
  "tools": [
    { "id": "tool_pesquisa", "name": "Pesquisa", "type": "transfer", "description": "Pesquisa de satisfação" },
    { "id": "tool_atendente", "name": "Codigo de rastreio", "type": "transfer", "description": "Encaminhar a humano" }
  ],
  "callback_url": "https://api.vonix.com/flows/turn/4f7a2c1e-9b3d-4c2a-8e1f-7a6b5c4d3e2f/callback"
}
```

> A parte final da `callback_url` (`4f7a2c1e-...`) é o token UUID. O agente **não monta** esse token — usa a URL inteira exatamente como veio.

### 2. O agente responde 202 na hora — sem corpo, sem esperar o Gemini

```
HTTP/1.1 202 Accepted
```

Nesse instante o handler já registrou a tarefa em background (`background_tasks.add_task(processar_e_entregar, payload)`). A plataforma desbloqueia e fica aguardando a entrega no `callback_url`.

### 3. Em background, `processar_turno` roda

- consulta `https://api-frases-teste.onrender.com/retornorastreios`
- monta o prompt (rastreios + ferramentas + histórico)
- chama o Gemini, que devolve algo como:

```json
{
  "messages": ["Boa, Maria! Seu pedido BR123456 está em trânsito, previsão de entrega 30/10. Precisa de mais alguma coisa?"],
  "tags": ["status_pedido"],
  "exit": null
}
```

### 4. O código transforma no formato Vonix (em `processar_turno`)

- `messages` vira `[{ "type": "TEXT", "text": "..." }]`
- filtra mensagens vazias e limita a 10 itens (limite do contrato da API de callback)
- se `return_variables` pedir `note` e vier `exit.note`, injeta em `variables`

Como nesse caso `exit` veio `null` (continua conversando), `variables` não é preenchido.

### 5. O agente entrega o resultado — `POST` para a `callback_url` original, sem header de auth

```json
{
  "messages": [
    { "type": "TEXT", "text": "Boa, Maria! Seu pedido BR123456 está em trânsito, previsão de entrega 30/10. Precisa de mais alguma coisa?" }
  ],
  "tags": ["status_pedido"],
  "exit": null
}
```

A plataforma valida e aplica: o cliente recebe a mensagem no WhatsApp e o chat continua no mesmo bloco.

---

## Cenário alternativo: o cliente encerra e o agente sai do bloco

- **Quando** o cliente responde "Não, obrigado!" após receber o rastreio
- **Então** o Gemini devolve `exit` preenchido (ex.: ferramenta `Pesquisa`)
- **E** o agente entrega via callback com `exit` e `variables.note`

```json
{
  "messages": ["Perfeito! Vou te direcionar pra uma pesquisa rápida. Valeu!"],
  "tags": ["status_pedido"],
  "variables": { "note": "Cliente consultou BR123456, satisfeito, direcionado à pesquisa" },
  "exit": { "tool": "tool_pesquisa", "note": "Cliente consultou BR123456, satisfeito, direcionado à pesquisa" }
}
```

A plataforma lê `exit.tool`, sai do bloco pelo output correspondente (`Pesquisa`) e o próximo bloco pode usar `{{ctx.note}}` para herdar o contexto.

---

## Cenários de erro

### A. Token expirado / turno já encerrado — `409`

- **Dado** que o processamento demorou mais que o `callback_deadline`
- **Quando** o agente tenta `POST` na `callback_url`
- **Então** a plataforma devolve `409` com `state: expired` (ou `already_resolved` / `chat_closed`)
- **E** o agente **não retenta** (o turno não reabre)
- **E** o chat segue pela saída **Fallback** do bloco na Vonix

Resposta do agente ao receber 409 (no log, sem expor a URL):

```
[callback] turno indisponível (status 409); não será reenviado
```

### B. Falha no processamento (Gemini indisponível) — Fallback silencioso

- **Dado** que todos os modelos Gemini falharam (503 exaustas)
- **Quando** `processar_turno` levanta exceção
- **Então** não há corpo válido para enviar à `callback_url`
- **E** o turno expira sozinho no `callback_deadline`
- **E** o chat segue pela saída **Fallback** do bloco
- **E** o erro é logado via `traceback.print_exc()`, sem reenviar

### C. Corpo inválido / muito grande — `400` ou `413`

- **Quando** o `POST` tem corpo que não é JSON válido (`400`) ou acima de 1 MB (`413`)
- **Então** o agente **não retenta** (não se resolve repetindo)
- **E** a plataforma aplica a saída **Fallback** do bloco

### D. Token desconhecido — `404`

- **Quando** a `callback_url` não corresponde a nenhum turno (token inválido/errado)
- **Então** a plataforma devolve `404`
- **E** o agente **não retenta**

### E. Falha interna temporária — `500` (única exceção que retenta)

- **Quando** a plataforma devolve `5xx`
- **Então** o agente faz **uma única** nova tentativa após 2s
- **E** se persistir, desiste (o turno segue encerrado, repetir não o reabre)

---

## Fluxo síncrono de fallback (sem `callback_url`)

Se o payload **não trouxer** `callback_url`, o agente mantém o comportamento do `main.py`:

- processa o turno
- devolve o resultado no corpo da resposta HTTP `200`

Isso serve para testes locais e para plataformas que esperam resposta no próprio corpo. A presença/ausência do campo `callback_url` é o que decide o fluxo.

---

## Por que o callback existe nesse caso

No fluxo síncrono (`200`), o cliente fica esperando enquanto o agente chama o Gemini — e se passar do tempo limite da plataforma, o turno quebra. No fluxo de callback o agente devolve `202` em milissegundos ("recebi") e só depois faz o trabalho pesado. O cliente não percebe a diferença no WhatsApp, mas o sistema fica muito mais robusto a lentidão do modelo.

---

## Regras de segurança/contrato respeitadas pelo código

- **Sem header de autorização** no POST — a própria `callback_url` é a credencial por turno.
- **URL nunca logada** — nos logs aparece só o tipo do erro (`type(e).__name__`) e o status code.
- **Uso único respeitado** — não retenta em `409` (turno já encerrado) nem `404` (token desconhecido).
- **`messages` limitado a 10 itens** (além disso a resposta é inválida e cai no Fallback).
- **`variables` montada em Python** (fora do schema do Gemini), injetando `exit.note` em `note` quando solicitado pelo `return_variables`.
- **O token final da URL não é montado pelo agente** — o POST usa a `callback_url` exatamente como recebida.

---

## Limitação conhecida

O `BackgroundTasks` roda no mesmo processo do worker. Se o Render reciclar o worker antes de o processamento terminar, o turno fica pendente até o `callback_deadline` expirar (o chat segue pelo Fallback do bloco). Para resiliência total seria necessária uma fila (RQ/Celery) num worker separado — fora do escopo atual. O `generate_content` do Gemini também é síncrono (igual ao `main.py`), bloqueando o event loop durante a chamada; se virar gargalo, dá pra trocar por `asyncio.to_thread`.
