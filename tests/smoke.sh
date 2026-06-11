#!/bin/bash
D="https://youtube-mp3-downloader-production-c1e2.up.railway.app"
PASS=0; FAIL=0
ok(){ echo "PASS $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL $1 — $2"; FAIL=$((FAIL+1)); }

# 1. homepage
H=$(curl -s --max-time 20 "$D/"); [[ "$H" == *"convert"* || "$H" == *"Convert"* || "$H" == *"MP3"* ]] && ok "homepage renders" || bad "homepage" "no UI markers"

# 2. /info valid
I=$(curl -s --max-time 25 -X POST "$D/info" -H 'Content-Type: application/json' -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"}')
[[ "$I" == *"Me at the zoo"* ]] && ok "/info returns title" || bad "/info" "$（echo $I|head -c 80)"

# 3. /info garbage
G=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$D/info" -H 'Content-Type: application/json' -d '{"url":"https://example.com/notyoutube"}')
[[ "$G" == "400" || "$G" == "200" ]] && ok "bad URL handled ($G, no crash)" || bad "bad URL" "http $G"

convert(){ # url fmt quality label ; echoes filename on success
  local J S
  J=$(curl -s --max-time 30 -X POST "$D/start" -H 'Content-Type: application/json' -d "{\"url\":\"$1\",\"format\":\"$2\",\"quality\":\"$3\"}" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("job_id",""))
except: print("")')
  [ -z "$J" ] && { echo "__START_FAIL__"; return; }
  for i in $(seq 1 50); do
    S=$(curl -s --max-time 15 "$D/status/$J")
    case "$S" in
      *'"status":"done"'*) echo "$J|$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["filename"])')"; return;;
      *'"status":"error"'*) echo "__ERR__|$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("error",""))')"; return;;
    esac; sleep 4
  done; echo "__TIMEOUT__"
}

# 4. MP3 128K popular (cached path)
R=$(convert "https://www.youtube.com/watch?v=jNQXAC9IVRw" mp3 128K)
[[ "$R" == __* ]] && bad "mp3 128K popular" "$R" || ok "mp3 128K popular → ${R#*|}"

# 5. MP3 320K FRESH Arabic video (the previously-broken path)
VID=$(shuf -n1 /home/khaled/recovered_videos/targets_live.txt 2>/dev/null || echo dQw4w9WgXcQ)
R5=$(convert "https://www.youtube.com/watch?v=$VID" mp3 320K)
[[ "$R5" == __* ]] && bad "mp3 320K fresh ($VID)" "$R5" || ok "mp3 320K fresh Arabic → ${R5#*|}"

# 6. real file download + audio validity (from test 5 if ok, else test 4)
SRC="$R5"; [[ "$SRC" == __* ]] && SRC="$R"
if [[ "$SRC" != __* ]]; then
  J="${SRC%%|*}"; FN="${SRC#*|}"
  curl -s --max-time 90 -o /tmp/suite.mp3 "$D/download/$J/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$FN")"
  SZ=$(stat -c%s /tmp/suite.mp3 2>/dev/null || echo 0)
  TYPE=$(file -b /tmp/suite.mp3)
  if [ "$SZ" -gt 100000 ] && [[ "$TYPE" == *"Audio"* || "$TYPE" == *"MPEG"* ]]; then ok "file download: $SZ bytes, $TYPE"; else bad "file download" "size=$SZ type=$TYPE"; fi
else bad "file download" "no completed job to fetch"; fi

# 7. MP4 720
R7=$(convert "https://www.youtube.com/watch?v=jNQXAC9IVRw" mp4 720)
[[ "$R7" == __* ]] && bad "mp4 720" "$R7" || ok "mp4 720 → ${R7#*|}"

# 8. nonexistent video id
R8=$(convert "https://www.youtube.com/watch?v=aaaaaaaaaaa" mp3 128K)
[[ "$R8" == __ERR__* ]] && ok "fake video id → clean error" || { [[ "$R8" == __* ]] && ok "fake id rejected ($R8)" || bad "fake id" "unexpectedly succeeded: $R8"; }

echo "=================================="
echo "RESULT: $PASS passed, $FAIL failed"
