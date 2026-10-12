---
title: EURUSD Engine Pro
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# EURUSD Engine Pro

Tableau de recherche EUR/USD responsive, en français, avec analyse multi-timeframe, EMA, balayages de liquidité/reclaim, FVG candidates, Order Blocks heuristiques, niveaux indicatifs et backtests comparatifs.

## Ce que le projet contient

- `app.py` : application Flask, API et moteur de règles.
- `templates/index.html` : interface web.
- `static/style.css` et `static/app.js` : interface responsive et interactions.
- `requirements.txt` : dépendances Python.
- `render.yaml` : configuration de déploiement Render.
- `Dockerfile` : configuration d'hébergement Docker, notamment Hugging Face Spaces.

## API

- `/api/health` — santé du serveur.
- `/api/analysis` — analyse multi-timeframe H1/M15/M5/M1.
- `/api/backtest?days=30&cost_pips=1.2&strategy=ema` — test de base.
- `/api/backtest?days=30&cost_pips=1.2&strategy=sweep` — sweep/reclaim + tendance.
- `/api/backtest?days=30&cost_pips=1.2&strategy=sweep_displacement` — sweep/reclaim + tendance + displacement.

Les stratégies de backtest disponibles sont `ema`, `sweep` et `sweep_displacement`. Le dernier est un proxy Price Action et non une reconstruction complète des Order Blocks/FVG.

## Déploiement A — Render

1. Téléverser le contenu du dossier à la racine d'un dépôt GitHub.
2. Dans Render, choisir **New + → Blueprint** et sélectionner le dépôt.
3. Render lit `render.yaml`, installe les dépendances et lance Gunicorn.
4. Après déploiement, tester `/api/health`, `/api/analysis` et `/api/backtest?days=30&cost_pips=1.2&strategy=sweep`.

## Déploiement B — Hugging Face Spaces (Docker)

1. Créer un compte sur Hugging Face, puis ouvrir `https://huggingface.co/new-space`.
2. Donner un nom au Space et choisir la visibilité voulue.
3. Choisir **Docker** comme SDK.
4. Créer le Space, puis téléverser tous les fichiers et dossiers de ce projet à la racine du dépôt du Space, y compris le `Dockerfile` et ce `README.md`.
5. Attendre la construction. Le Space doit écouter sur le port `7860`, configuré dans le Dockerfile.
6. Ouvrir le Space : son URL publique est le lien de l'application. Tester `/api/health` à la fin de l'URL.

## Sources et limites importantes

- Les bougies viennent du point d'accès public Yahoo Finance pour `EURUSD=X`. Il ne s'agit pas d'un flux institutionnel centralisé, et la disponibilité, le retard ou l'historique peuvent changer.
- Le volume FX Yahoo ne doit pas être assimilé à un volume centralisé. Le VWAP n'est affiché que si la source fournit un volume exploitable; aucun volume n'est inventé.
- Les Order Blocks, FVG et balayages sont des heuristiques explicables, pas des labels universels.
- Les backtests sont approximatifs : coûts constants, pas de bid/ask historique, slippage, commissions ou rollover réalistes; ils ne valident pas une rentabilité future.
- Aucun ordre n'est exécuté. Le site ne remplace pas la vérification sur le courtier, les tests hors échantillon et le contrôle du risque.

## Exécution locale

```bash
pip install -r requirements.txt
python app.py
```

Ouvrir ensuite `http://127.0.0.1:5000`.
