# Como entregar a resposta final de um atendimento (callback)

## A ideia em uma frase

Quando seu sistema recebe um pedido de atendimento, ele pode responder de imediato só dizendo "recebi, vou processar" (status **202**). A resposta final, com o que o cliente vai ler, é entregue depois, em um segundo momento, por um endereço especial chamado **callback_url**.

## A analogia do balcão de lanchonete

Imagine que você faz um pedido numa lanchonete e recebe uma **senha numerada**. A pessoa do balcão diz: "anotado, daqui a pouco chamamos seu número." Esse é o **202** — só confirma que recebeu o pedido, sem ainda entregar o lanche.

Quando o lanche fica pronto, ela **chama seu número** e você recebe. A senha:

- **só serve para aquele pedido**
- **vale uma única vez** — depois de usado, não funciona mais
- **é pessoal daquele pedido** — funciona como uma chave, sem precisar de senha extra

No nosso sistema, o "número da senha" é a **callback_url**. Seu sistema usa esse endereço **uma única vez** para entregar a resposta final do atendimento.

## Regras de ouro da callback_url

- **Use exatamente o endereço que você recebeu.** Não tente montar ou adivinhar o final dele (é um código único gerado pelo sistema).
- **É uma chave descartável.** Funciona como credencial: ela mesma já autoriza o envio, **sem precisar de senha/token adicional**.
- **Não guarde.** Não salve em banco nem deixe vazar em log compartilhado — use, entregue, descarte.
- **Só vale uma vez.** Se você mandar duas vezes, a segunda recebe um erro **409** (já encerrado).

## O que você envia de volta (o conteúdo da entrega)

Quando chamar a callback_url, mande um JSON com até 4 partes:

- **`messages`** — as mensagens que o cliente vai receber. No máximo **10**, na ordem em que devem aparecer.
- **`variables`** — valores a salvar no fluxo (ex.: número do protocolo). Só o que estiver na lista permitida é salvo; o resto é ignorado. Aceita texto, número ou verdadeiro/falso. Não pode ser vazio (`null`), lista ou objeto.
- **`tags`** — marcadores do atendimento (ex.: `"segunda_via"`). **Tag vazia invalida tudo.**
- **`exit`** — a saída do bloco. No máximo **uma** por resposta. Se vier vazio, o cliente **continua no mesmo bloco** conversando.

## As respostas que você recebe de volta

- **202 — Recebido.** Sua resposta será validada e aplicada depois. Se estiver inválida, o chat segue pelo caminho de **Fallback** (saída de emergência do bloco).
- **400 — Corpo inválido.** O que você mandou não é um JSON válido.
- **404 — Token desconhecido.** O final da URL não corresponde a nenhum atendimento.
- **409 — Turno já encerrado.** Não dá para usar de novo. O motivo vem no campo `state`:
  - `already_resolved` — você já usou essa callback antes
  - `expired` — estourou o tempo limite de entrega
  - `chat_closed` — o cliente já saiu desse bloco
- **413 — Arquivo grande demais.** O corpo passou de 1 MB.
- **500 — Falha interna.** Pode tentar de novo, mas isso **não reabre** o turno (ele segue encerrado).

## Resumo para não esquecer

> Recebe o atendimento → responde 202 ("recebi") → processa → entrega a resposta pela callback_url **uma única vez**, sem senha extra, do jeito que veio o endereço. Deu erro 409 ou 404? Não insista, o turno já acabou.
