# Fixtures do radar de produto

Respostas locais para `product_radar.py <instância> --dry --fixtures <esta pasta>` e para os testes: cada URL vira um
arquivo `<host>/<caminho>[__<query>]` (`radar.fixture_path`; uma data na query vira `DATE`). Dados sintéticos: produtos,
lançamentos e repositórios são exemplos (`example.com`, `example-org`), não notícias reais. Um feed sem arquivo aqui é
tratado como "sem novidade"; um tópico do GitHub sem arquivo é uma falha da fonte.
