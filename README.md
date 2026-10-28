# AgenteIAEcommerce

Endpoint acionado pelo seu backend após o recebimento do status 202.
Uso da URL: Utilize a callback_url exatamente como foi recebida (o caminho https://{host}/chats/agent/callback/{token} é meramente ilustrativo).
Segurança: A URL atua como uma credencial de uso único. Não utilize headers de autorização, não a armazene e não a exiba em logs compartilhados.
Ciclo de vida: O link é de uso único. Uma segunda tentativa de chamada resultará em um erro 409 Conflict

path Parameters
token (required)
É o código identificador que fica no final da callback_url (assim como o identificador único gerado pelo Webhook.site). Não tente montar essa URL programaticamente no seu código (ex: concatenando strings ou IDs). Utilize sempre a URL completa exatamente como ela foi entregue pela plataforma no payload do turno.

Request Body (application/json)
messages (Array de objetos ResponseMessage, máx. 10 itens): Lista de até 10 mensagens a serem enviadas ao contato, rigorosamente na ordem em que aparecem na lista. Atenção: Arrays vazios ou com mais de 10 itens invalidam a resposta.
variables (Object)
Dicionário de variáveis a serem gravadas no fluxo. Regra importante: Apenas as chaves previamente listadas em return_variables serão efetivamente salvas; todas as outras serão ignoradas. Os valores permitidos devem ser estritamente string, número ou booleano. Enviar null, objetos ou arrays tornará a resposta inválida. Variáveis não gravadas receberão o valor padrão configurado no bloco ao sair.
tags (Array de strings)
Lista de tags aplicadas ao atendimento atual. Atenção: O envio de uma string vazia ("") invalidará a resposta.
exit (Object Exit, opcional)
Controla a saída do bloco de atendimento atual (permitido no máximo um objeto por resposta). Se omitido ou nulo, o chat continuará conversando normalmente no bloco atual.