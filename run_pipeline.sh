#!/bin/zsh
# 依序跑：top200 動能選股 → 飆漲股 DNA 研究。即時輸出到各自 log。
cd /Users/hankchung/Dev/tw-swing-factor
PY=.venv/bin/python

# 先建候選池(否則乾淨 checkout / 清過 outputs 會 fail;既有環境也避免沿用過期池)
echo "[pipeline] $(date '+%H:%M:%S') 建候選池 top200 / top300 …"
$PY -u build_universe.py 200 > outputs/_universe_build.log 2>&1
$PY -u build_universe.py 300 >> outputs/_universe_build.log 2>&1

echo "[pipeline] $(date '+%H:%M:%S') 開始 top200 選股 …"
$PY -u main.py screen --pool 200 > outputs/_screen_live.log 2>&1
echo "[pipeline] $(date '+%H:%M:%S') 選股結束 (exit=$?)，開始飆漲 DNA 研究 …"

$PY -u winner_dna.py --gain 0.30 --win 60 --pool 300 > outputs/_dna_live.log 2>&1
echo "[pipeline] $(date '+%H:%M:%S') DNA 研究結束 (exit=$?)。全部完成。"
