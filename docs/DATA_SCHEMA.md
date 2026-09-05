# Schema do payload `data.json`

O arquivo `site/public/data.json` é gerado por `scripts/build_data_json.py` a
partir do banco SQLite `work/rfb-pharma.db`. É a única fonte de dados do
front-end.

## Topo

| Campo           | Tipo   | Descrição                                      |
|-----------------|--------|-------------------------------------------------|
| `cycle`         | string | Ciclo da base Receita (ex.: `"2026-08"`)        |
| `generated_at`  | string (ISO 8601) | Timestamp UTC de geração do payload |
| `counts`        | objeto | Estatísticas agregadas (ver abaixo)             |
| `geocoded`      | objeto | Cobertura da geocodificação (ver abaixo)        |
| `delta`         | objeto | Variações em relação ao ciclo anterior          |
| `establishments`| array  | Lista completa de estabelecimentos              |

## `counts`

| Campo            | Tipo                  | Descrição                                    |
|------------------|-----------------------|-----------------------------------------------|
| `total`          | int                   | Total de estabelecimentos                     |
| `farmacia`       | int                   | Subtotal CNAE 4771-7                          |
| `cosmeticos`     | int                   | Subtotal CNAE 4772-5                          |
| `by_municipio`   | `{string: int}`       | Contagem por município                        |
| `by_situacao`    | `{string: int}`       | Contagem por situação cadastral (RFB)         |

## `geocoded`

| Campo          | Tipo   | Descrição                                          |
|----------------|--------|-----------------------------------------------------|
| `count`        | int    | Estabelecimentos com `lat`/`lon` resolvidos         |
| `total`        | int    | Total de estabelecimentos                           |
| `coverage_pct` | float  | Porcentagem de cobertura (`count` / `total`)         |

## `delta`

Resumo e linhas detalhadas das variações entre o ciclo atual e o anterior:

- `novas` (int) — CNPJs novos no ciclo
- `baixas` (int) — CNPJs que saíram da base
- `mudaram_endereco` (int) — CNPJs com alteração de endereço
- `mudaram_situacao` (int) — CNPJs com alteração de situação cadastral
- `total` (int) — soma dos anteriores
- `rows` (array) — eventos detalhados (ver abaixo)

## `establishments[]`

Cada elemento:

| Campo               | Tipo                        | Descrição                                       |
|---------------------|-----------------------------|--------------------------------------------------|
| `cnpj`              | string                      | CNPJ formatado (`XX.XXX.XXX/XXXX-XX`)           |
| `cnpj_raw`          | string                      | CNPJ sem pontuação (14 dígitos)                  |
| `matriz_filial`     | `"Matriz"` \| `"Filial"`    | Tipo do estabelecimento                          |
| `matriz_filial_code`| `"1"` \| `"2"`              | Código original RFB                              |
| `razao_social`      | string                      | Razão social                                     |
| `nome_fantasia`     | string                      | Nome fantasia                                    |
| `layer`             | `"FARMACIA"` \| `"COSMETICOS"` | Camada no mapa                                |
| `layer_label`       | string                      | Rótulo amigável                                  |
| `cnae_principal`    | string                      | CNAE principal                                   |
| `cnae_secundaria`   | string                      | CNAEs secundárias (CSV)                          |
| `situacao`          | string                      | Situação cadastral (`ATIVA`, `BAIXADA`, ...)     |
| `situacao_code`     | string                      | Código original RFB                              |
| `situacao_data`     | string                      | Data da situação (AAAA-MM-DD)                    |
| `motivo_situacao`   | string                      | Motivo da situação                               |
| `abertura_data`     | string                      | Data de abertura (AAAA-MM-DD)                    |
| `municipio_codigo`  | string                      | Código TOM/TCE do município (RFB)                |
| `municipio_nome`    | string                      | Nome do município (Title Case)                   |
| `uf`                | string                      | UF (sempre `SP` para a Baixada Santista)         |
| `logradouro_tipo`   | string                      | Tipo (Rua, Av., ...)                             |
| `logradouro`        | string                      | Nome do logradouro                               |
| `logradouro_full`   | string                      | Tipo + logradouro concatenados                   |
| `numero`            | string                      | Número                                           |
| `complemento`       | string                      | Complemento                                      |
| `bairro`            | string                      | Bairro                                           |
| `cep`              | string                      | CEP                                              |
| `telefone1`         | string                      | Telefone 1 (`(DDD) NÚMERO`)                      |
| `telefone2`         | string                      | Telefone 2                                       |
| `email`             | string                      | E-mail                                           |
| `capital_social`    | number \| null              | Capital social (R$)                              |
| `porte`             | string                      | Porte da empresa                                 |
| `simples`           | string                      | Optante pelo Simples (`Sim` / `Não`)             |
| `mei`               | string                      | Optante pelo MEI (`Sim` / `Não`)                 |
| `lat`               | number \| null              | Latitude (geocodificado)                         |
| `lon`               | number \| null              | Longitude (geocodificado)                        |
| `_key`              | string                      | Chave normalizada do endereço (cache do geocoder)|

## `delta.rows[]`

| Campo             | Tipo   | Descrição                              |
|-------------------|--------|-----------------------------------------|
| `cnpj`            | string | CNPJ formatado                         |
| `cnpj_raw`        | string | CNPJ sem pontuação                     |
| `kind`            | string | `NOVA` \| `BAIXA` \| `MUDOU_ENDERECO` \| `MUDOU_SITUACAO` |
| `detail_json`     | string | Detalhes em JSON (varia conforme `kind`)|
| `detected_at`     | string | ISO 8601                               |
| `municipio_nome`  | string | Município                              |
| `razao_social`    | string | Razão social                           |
| `nome_fantasia`   | string | Nome fantasia                          |
| `situacao`        | string | Situação cadastral                     |
| `cnae_principal`  | string | CNAE principal                         |
