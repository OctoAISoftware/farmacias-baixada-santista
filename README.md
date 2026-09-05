# Mapa de Farmácias — Baixada Santista

Mapa interativo dos estabelecimentos de **farmácia** (CNAE 4771-7) e
**cosméticos/perfumaria** (CNAE 4772-5) dos 9 municípios da **Baixada
Santista** (Santos, São Vicente, Guarujá, Praia Grande, Cubatão, Bertioga,
Itanhaém, Mongaguá e Peruíbe), alimentado mensalmente pela base pública de
CNPjs da Receita Federal.

O projeto combina:

- um **pipeline de carga mensal** que baixa a base oficial aberta de CNPJs
  da Receita Federal, filtra os CNAEs de farmácia e cosméticos nos municípios
  da Baixada Santista e gera um banco SQLite local;
- um **geocoder incremental** que resolve coordenadas (lat/lon) dos
  endereços usando o serviço público Photon (Komoot/OSM), com cache
  persistente em arquivo JSON;
- um **site estático** (Next.js + React + Tailwind + Leaflet) que renderiza o
  mapa interativo com clustering de marcadores, filtros por município /
  categoria / situação cadastral / tipo (matriz/filial) e estatísticas;
- um **agendador mensal** (cron) que reaplica o pipeline automaticamente.

A base de origem é inteiramente pública (Receita Federal, dados abertos) e os
tiles do mapa são do OpenStreetMap. Nenhuma chave de API é necessária para
rodar o projeto.

## Estrutura do repositório

```
.
├── site/                         Aplicação Next.js (mapa interativo)
│   ├── app/
│   │   ├── layout.tsx            Layout raiz, metadados e fontes
│   │   ├── page.tsx              Página principal (carrega data.json)
│   │   └── globals.css           Estilos globais (Tailwind)
│   ├── components/
│   │   └── FarmaciasMap.tsx      Componente do mapa (Leaflet + clustering)
│   ├── package.json
│   ├── next.config.mjs
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   └── postcss.config.mjs
│
├── scripts/                      Pipeline Python
│   ├── run-pipeline.py           Download (resumível) + parse + SQLite
│   ├── build_data_json.py        Gera site/public/data.json a partir do SQLite
│   ├── geocode_nominatim.py      Geocodificação em background com cache
│   ├── etl_farmacias.py          ETL alternativo (XLSX, sem SQLite)
│   ├── rebuild_estab_snap.py     Reaplica carga de estabelecimentos
│   └── download-cnpj.sh          Download sequencial via curl (referência)
│
├── monthly-rfb-update.sh         Entrypoint para cron mensal
├── sample_data/
│   └── data.sample.json          Amostra do payload servido pelo site
├── docs/
│   └── DATA_SCHEMA.md            Esquema do JSON entregue ao front
├── requirements.txt              Dependências Python do pipeline
├── package.json (referência)     Apenas para o site em site/
└── .gitignore
```

## Pré-requisitos

- Python 3.10+
- Node.js 18+ e `npm` (para o site)
- `curl` (para o download sequencial de referência)
- acesso de saída HTTPS para `arquivos.receitafederal.gov.br`,
  `photon.komoot.io` e `tile.openstreetmap.org`

## Instalação

```bash
# 1. Dependências Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Dependências Node (site)
cd site
npm install
cd ..
```

## Como rodar o pipeline (carga mensal)

O pipeline gera, em `work/`:

- `rfb-pharma.db` — banco SQLite com os snapshots do ciclo atual
- `status.json` — status de progresso (downloads + parse)
- `geocode/cache.json` — cache persistente do geocoder
- `output/` — XLSX gerado (via `etl_farmacias.py`)

Etapas (cada uma pode ser rodada independentemente):

```bash
# 1) Download resumível da base RFB do ciclo AAAA-MM
#    (gera arquivos em ./downloads/, ~7 GB no total)
python3 scripts/run-pipeline.py download 2026-08

# 2) Parse → carrega no SQLite local e gera XLSX
python3 scripts/run-pipeline.py parse 2026-08

# 3) Gera o JSON consumido pelo site (site/public/data.json)
python3 scripts/build_data_json.py --cycle 2026-08

# 4) Geocodificação em background (Photon, cache persistente)
#    Pode rodar em paralelo ao site — o cache é consultado a cada
#    re-geração de data.json
python3 scripts/geocode_nominatim.py --cycle 2026-08
```

A entrada completa (download → parse → data.json → geocoder) pode ser
executada de uma vez pelo script `monthly-rfb-update.sh`:

```bash
./monthly-rfb-update.sh 2026-08
```

### Configuração por variáveis de ambiente

Todos os scripts aceitam:

| Variável            | Padrão                                     | Descrição                                     |
|---------------------|--------------------------------------------|-----------------------------------------------|
| `PROJECT_ROOT`      | diretório do projeto                       | Raiz onde ficam `downloads/`, `work/`, `site/`|
| `DL_ROOT`           | `<PROJECT_ROOT>/downloads`                 | Onde os `.zip` da Receita são baixados        |
| `OUT_DIR`           | `<PROJECT_ROOT>/output`                    | Onde os XLSX são escritos                     |
| `CYCLE`             | `2026-08`                                  | Ciclo (AAAA-MM) da base Receita               |
| `GEOCODER_UA`       | `farmacias-baixada-santista/1.0`           | User-Agent do geocoder                        |
| `GEOCODER_SLEEP_S`  | `0.4`                                      | Intervalo entre requisições Photon (segundos) |

## Como rodar o site (mapa)

O site é uma aplicação Next.js padrão (App Router) que serve, a partir de
`site/public/data.json`, o mapa e os dados renderizados.

```bash
cd site
npm run dev     # desenvolvimento em http://localhost:3000
# ou
npm run build && npm start     # produção em http://localhost:3000
```

Para gerar o `data.json` antes de subir o site:

```bash
# (após rodar o pipeline)
python3 scripts/build_data_json.py --cycle 2026-08
# o arquivo é gravado em site/public/data.json
```

## Atualização mensal automática

A pipeline foi desenhada para ser executada mensalmente via `cron`. Exemplo
de entrada em `crontab` (todo dia 1º às 06:00, fuso America/Sao_Paulo):

```cron
0 6 1 * * TZ=America/Sao_Paulo /opt/farmacias-baixada-santista/monthly-rfb-update.sh
```

O script detecta automaticamente o ciclo anterior caso não receba o argumento
`AAAA-MM`.

## Origem dos dados

- **Base de CNPjs:** Receita Federal — dados abertos
  (<https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ/>).
- **Geocodificação:** Photon (Komoot/OSM)
  (<https://photon.komoot.io/api/>), com fallback opcional para Nominatim
  (OpenStreetMap).
- **Tiles do mapa:** OpenStreetMap contributors
  (<https://tile.openstreetmap.org>).

## Licença

Código-fonte: MIT — ver [LICENSE](LICENSE).

A base de CNPjs da Receita Federal é de domínio público; os tiles do mapa são
© OpenStreetMap contributors (ODbL). Verifique as condições de uso dos
respectivos provedores antes de redistribuir.
