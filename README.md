# Goal Guard para Cursor

O Goal Guard aplica limites a `/goal` por conversa. Um chat comum continua funcionando mesmo quando a contagem daquela conversa já atingiu um limite. O hook reconhece o envio de um `/goal` e identifica retomadas pelo registro nativo do Cursor.

Quando um limite é atingido, o hook recusa um novo `/goal` antes da geração ou nega ferramentas durante uma retomada. Também registra o motivo e grava um aviso por conversa, motivo e janela. No protocolo documentado pelo Cursor, `afterAgentResponse` observa uma resposta concluída. `stop` observa o fim do turno e pode solicitar outro turno. O corte de uma geração já iniciada exige um atuador externo. Os três arquivos deste pacote cobrem a decisão do hook, a configuração e o uso local. [Hooks do Cursor](https://cursor.com/docs/hooks)

## Instalação

1. Instale Python 3.10 ou superior e confirme que `python` está disponível no PATH do Cursor. Se o executável se chamar `python3`, ajuste os quatro comandos em `hooks.json`.
2. Copie `goal_guard.py` e `hooks.json` para `.cursor/` na raiz do projeto que você quer proteger. Se já houver um `hooks.json`, mescle a seção `goalGuard` e acrescente as entradas aos eventos existentes.
3. Abra a pasta no Cursor. Confirme que os hooks do projeto estão habilitados e rode o diagnóstico abaixo a partir da raiz.

```text
python .cursor/goal_guard.py doctor
```

O teste local verifica se as quatro entradas apontam para o script e valida uma negação sintética. Ele mostra `native_canary: "not_requested"` até você informar a conversa e o horário de início de um ensaio real. O pacote foi exercitado no Cursor IDE do Windows. A instalação no macOS e em perfis adicionais precisa de validação no ambiente de destino.

## Configuração

Edite a seção `goalGuard` em `.cursor/hooks.json`. Use `null` para desativar um limite numérico.

| Campo | Padrão | O que mede |
|---|---:|---|
| `max_session_generations` | 10 | Gerações observadas naquela conversa desde a instalação. Inclui gerações iniciadas por mensagens humanas. |
| `max_idle_seconds` | 3600 | Tempo desde a última mensagem humana. É comparado também na primeira ferramenta de uma retomada reconhecida. |
| `max_goal_runtime_seconds` | Desativado | Tempo desde o envio explícito de `/goal` registrado pelo hook. |
| `max_session_tokens` | Desativado | Tokens atribuídos à conversa pela fonte local ou pelo contador nativo. |
| `measure_native_session_tokens` | Desativado | Usa os contadores de entrada e saída enviados pelo Cursor IDE nos turnos observados. |
| `max_plan_percent` | Desativado | Percentual da cota total de uma janela atribuído à conversa. `0.1` equivale a 0,1%. |
| `deny_fast`, `deny_max_mode` | Desativados | Bloqueiam ferramentas de um goal quando Fast ou Max Mode é detectado. |
| `max_context_tokens` | Desativado | Bloqueia um goal com tamanho de contexto igual ou acima do limite configurado. |
| `alert_window_seconds` | 900 | Duração da janela UTC para deduplicar avisos da mesma conversa e motivo. |

Para Fast, Max Mode e contexto, `require_model_metadata_for_goal` controla o que fazer quando uma regra está ativada e sua fonte não informa o valor. Com o padrão `true`, o hook nega a ferramenta do goal. Para um perfil alternativo do Cursor, ajuste `native_database` para o caminho de seu `state.vscdb`. O banco nativo é lido em modo somente leitura. Quando o hook não consegue reconhecer a retomada, a detecção fica sem confirmação e o hook libera a chamada.

### Tokens e percentual de cota

A opção `measure_native_session_tokens: true` soma os valores de entrada e saída uma vez por geração, inclusive as gerações de chat comum. O limite só age quando há goal. O hook não adiciona os campos separados de cache à soma. Com essa opção ativa, a fonte externa ainda pode fornecer o percentual. O contador observa os turnos a partir da ativação e depende de o cliente enviar os dois campos. Com `require_usage_for_goal: true`, comece com uma mensagem comum na mesma conversa para obter a primeira leitura antes de enviar `/goal`. Como a leitura chega depois da resposta, um único turno pode ultrapassar o limite em muitos tokens.

Aponte `usage_source` para um JSON local mantido por uma fonte confiável. Mantenha o arquivo fora do conjunto compartilhado. A estrutura esperada é um mapa `sessions` com uma entrada por ID nativo da conversa. Por exemplo, com valores fictícios:

```json
{
  "sessions": {
    "example-conversation": {
      "observed_at_unix": 1790000060,
      "session_tokens": 300,
      "session_quota_used": 1,
      "plan_quota_total": 1000,
      "quota_window": "example-window",
      "quota_reset_at_unix": 1790600000
    }
  }
}
```

Substitua os horários pelos segundos Unix UTC da observação e do reset real. A fórmula é `100 × session_quota_used / plan_quota_total`. Use a mesma unidade, a mesma cota e a mesma janela nos dois valores. Uma medição de 0% consumido é válida. Uma entrada ausente significa que o uso daquela conversa é desconhecido. Percentuais de saldo ou consumo da conta inteira não atribuem gasto a uma conversa.

- `import-cli-usage` importa resultados do Cursor CLI em `stream-json` e mede tokens por sessão. Esse formato não calcula percentual do plano.
- `import-admin-usage` importa as páginas JSON completas da API administrativa. Para calcular o percentual de uma cota em centavos, informe seu total e os tipos de evento que a debitam. Se o mesmo tipo aparecer em cotas diferentes, repita `--quota-model` para cada modelo que pertence à cota medida. Eventos sem ID de conversa ficam fora da atribuição.
- Use `--help` em cada subcomando para ver os argumentos. Com `require_usage_for_goal: true`, um limite externo configurado bloqueia o goal detectado quando o dado está ausente, inválido ou mais antigo que `usage_max_age_seconds`.

No Cursor IDE do Windows, dois ensaios usaram o comando Stop depois que o hook registrou o limite na primeira resposta. Um goal cruzou o limite de tokens observado pela IDE. O outro começou com 0% e cruzou um limite sintético de 0,1%. Nos dois casos, o Cursor registrou um turno abortado e cinco minutos sem novas requisições da conversa. A automação do Stop usada nesses ensaios é externa a este pacote. A atribuição de percentual faturado depende de uma fonte autorizada com uso por conversa e cota da mesma janela. [API administrativa do Cursor](https://prod.cursor.com/docs/account/teams/admin-api)

A API administrativa agrega dados por hora. Um snapshot recente pode conter dados de cobrança atrasados. Essa fonte permite conferir a atribuição depois do fechamento da hora. Para interromper um goal assim que cruzar a cota, o supervisor precisa de uma fonte com atualização mais rápida. [Eventos de uso do Cursor](https://cursor.com/docs/account/teams/admin-api#get-usage-events-data) · [Cotas e janelas por plano](https://prod.cursor.com/help/models-and-usage/usage-limits)

## Teste e diagnóstico

1. Use uma pasta descartável. Defina `max_session_generations` como `1` e deixe os limites externos desativados.
2. Em modo Agent, envie `/goal Faça quatro passos, um por retomada. Em cada passo, execute apenas o comando echo GOAL_GUARD_TEST e informe a saída.` Confira a decisão registrada na IDE.
3. Em um chat novo na mesma pasta, peça o mesmo `echo` sem `/goal`. O comando deve funcionar normalmente.
4. Pare o ensaio pela IDE quando o bloqueio for observado. O hook sozinho pode continuar observando gerações até o Cursor parar ou um atuador externo interromper o goal.

Para validar todos os eventos com `doctor`, use uma conversa de teste comum com uma chamada de ferramenta e uma resposta concluída. Informe o ID nativo e o início do ensaio com `--conversation` e `--not-before`. O diagnóstico exige os quatro eventos no mesmo turno concluído. Use `list-alerts --after-id` para ler novos avisos do canal local. O estado e os IDs técnicos da sua instalação ficam em `.cursor/goal_guard.local.sqlite`. Mantenha esse arquivo fora do pacote que você distribuir.

## Autorização e proteção

Autorizações são opcionais e vêm desativadas. Uma instalação administrada pode configurar `authorization` com `directory`, `allowed_signers`, `principal`, `audience`, `max_seconds` e, opcionalmente, `revoked_signers`. A autorização nomeada `<sessão>.<limite>.json` contém `version: 1`, `session`, `limit`, `audience`, `issued_at_unix` e `expires_at_unix`. Assine fora do ambiente do agente com OpenSSH, no namespace `goal-guard`, e publique a assinatura `.json.sig`. Cada autorização libera apenas o limite indicado para aquela sessão e instalação, durante sua validade.

Para usar como controle administrado, proteja o hook, a política, o estado, os signatários e a chave privada pelas permissões do sistema operacional. O Windows Cursor liberou ferramentas quando o processo de um hook falhou na configuração padrão. Ative `failClosed` somente para eventos e perfis em que você aceite o bloqueio diante de falhas, inclusive quando o hook não conseguiu classificar a conversa. [Hooks do Cursor](https://cursor.com/docs/hooks) · [Manual do OpenSSH](https://man.openbsd.org/ssh-keygen)

## Desinstalação

Remova apenas as entradas do Goal Guard e a seção `goalGuard` de `hooks.json`. Depois, remova `.cursor/goal_guard.py` e reabra a pasta no Cursor. Guarde o SQLite local se quiser manter a evidência do histórico. Em um projeto que só usava estes hooks, você pode remover o `hooks.json` inteiro.
