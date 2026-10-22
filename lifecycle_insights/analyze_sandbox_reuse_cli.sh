#!/usr/bin/env bash
# Understand whether and when users return to paused E2B sandboxes.
# Shell counterpart of analyze_sandbox_reuse.py. Read-only.
# Requires: curl, jq, E2B_API_KEY, and the `e2b` CLI for the current paused listing.

set -euo pipefail
umask 077

private_copy() {
  local source="$1" destination="$2" temporary
  if [[ -d "$destination" ]]; then
    echo "Export destination is a directory: $destination" >&2
    return 1
  fi
  temporary="$(mktemp "$(dirname -- "$destination")/.sandbox-export-XXXXXXXX")" || return 1
  if ! cat -- "$source" >"$temporary" || ! mv -f -- "$temporary" "$destination"; then
    rm -f -- "$temporary"
    return 1
  fi
}

windows="1,2,3,7"
since_days="7"
max_events="10000"
using_defaults=true
all_history=false
metadata=""
csv_path=""
skip_listing=false
events_file=""
save_events=""
domain="${E2B_DOMAIN:-e2b.app}"

usage() {
  cat >&2 <<USAGE
Usage: $0 [--windows 12h,1,2,3,7] [--since-days N] [--max-events N] [--all]
          [--metadata k=v,k2=v2] [--csv file] [--skip-listing]
          [--events-file file] [--save-events file]
Defaults: last 7 days and at most the newest 10000 events. --all removes both limits
(same as --since-days 0 --max-events 0). Plain window numbers are days; h/d suffixes allowed.
Read-only analysis of observed sandbox returns and pause/resume timing.
Exports apply history limits and metadata filters; JSON and CSV files are private (0600).
USAGE
  exit 2
}

while (($#)); do
  case "$1" in
    --windows) [[ $# -ge 2 ]] || usage; windows="$2"; shift 2 ;;
    --since-days) [[ $# -ge 2 ]] || usage; since_days="$2"; using_defaults=false; shift 2 ;;
    --max-events) [[ $# -ge 2 ]] || usage; max_events="$2"; using_defaults=false; shift 2 ;;
    --all) all_history=true; using_defaults=false; shift ;;
    --metadata) [[ $# -ge 2 ]] || usage; metadata="$2"; shift 2 ;;
    --csv) [[ $# -ge 2 ]] || usage; csv_path="$2"; shift 2 ;;
    --skip-listing) skip_listing=true; shift ;;
    --events-file) [[ $# -ge 2 ]] || usage; events_file="$2"; shift 2 ;;
    --save-events) [[ $# -ge 2 ]] || usage; save_events="$2"; shift 2 ;;
    *) usage ;;
  esac
done

# Validate --windows before touching credentials or the network.
if [[ -z "$windows" || "$windows" =~ (^|,)(,|$) ]]; then
  echo "invalid --windows value: empty entry; use positive durations like 12h,1,2,3,7" >&2; exit 2
fi
IFS=',' read -r -a window_tokens <<<"$windows"
for tok in "${window_tokens[@]}"; do
  low="$(tr '[:upper:]' '[:lower:]' <<<"${tok// /}")"
  if ! [[ "$low" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)[hd]?$ ]] || ! awk -v t="${low%[hd]}" 'BEGIN { exit !(t > 0) }'; then
    echo "invalid --windows value '$tok': use positive durations like 12h,1,2,3,7" >&2; exit 2
  fi
done
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

if [[ "$all_history" == true ]]; then
  since_days="0"; max_events="0"
fi
[[ "$since_days" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "--since-days must be a non-negative number" >&2; exit 2; }
[[ "$max_events" =~ ^[0-9]+$ ]] || { echo "--max-events must be a non-negative integer" >&2; exit 2; }

now_epoch="$(date -u +%s)"
since_epoch="null"
if [[ "$since_days" != 0 ]] && awk -v d="$since_days" 'BEGIN { exit !(d > 0) }'; then
  since_epoch="$(awk -v n="$now_epoch" -v d="$since_days" 'BEGIN { printf "%d", n - d * 86400 }')"
fi

# --windows tokens -> JSON [{label, seconds}] sorted ascending
windows_json="$(tr ',' '\n' <<<"$windows" | awk '
  { tok = tolower($0); gsub(/^ +| +$/, "", tok); if (tok == "") next
    if (tok ~ /h$/) { days = substr(tok, 1, length(tok) - 1) / 24 }
    else if (tok ~ /d$/) { days = substr(tok, 1, length(tok) - 1) + 0 }
    else { days = tok + 0 }
    hours = days * 24
    label = (days < 1 || hours % 24 != 0) ? sprintf("%gh", hours) : sprintf("%gd", days)
    printf "{\"label\":\"%s\",\"seconds\":%.0f}\n", label, days * 86400 }' \
  | jq -s 'unique_by(.seconds) | sort_by(.seconds)')"

# --metadata k=v,k2=v2 -> JSON object
meta_json="{}"
if [[ -n "$metadata" ]]; then
  meta_json="$(tr ',' '\n' <<<"$metadata" | jq -R 'split("=") | {(.[0]): (.[1:] | join("="))}' | jq -s 'add')"
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# ------------------------------------------------------------------ fetch events
events_json="$workdir/events.json"
if [[ -n "$events_file" ]]; then
  cp "$events_file" "$events_json"
else
  : "${E2B_API_KEY:?E2B_API_KEY is not set (needed to fetch events; not needed with --events-file)}"
  command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
  echo "Fetching lifecycle events..." >&2
  offset=0
  : >"$workdir/pages.jsonl"
  while :; do
    page="$(curl -sS --fail -H "X-API-Key: $E2B_API_KEY" -G "https://api.$domain/events/sandboxes" \
      --data-urlencode "limit=100" --data-urlencode "offset=$offset" \
      --data-urlencode "types=sandbox.lifecycle.created" --data-urlencode "types=sandbox.lifecycle.paused" \
      --data-urlencode "types=sandbox.lifecycle.resumed" --data-urlencode "types=sandbox.lifecycle.killed")"
    jq -c '.[]' <<<"$page" >>"$workdir/pages.jsonl"
    count="$(jq 'length' <<<"$page")"
    if ((count > 0)); then
      oldest="$(jq -r '.[-1].timestamp' <<<"$page")"
      echo "  fetched $(wc -l <"$workdir/pages.jsonl" | tr -d ' ') events, back to ${oldest%%.*}Z" >&2
      if [[ "$since_epoch" != null ]] && (( $(jq -r 'sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601' <<<"\"$oldest\"") < since_epoch )); then
        break
      fi
    fi
    if ((max_events > 0)) && (( $(wc -l <"$workdir/pages.jsonl") >= max_events )); then
      break
    fi
    ((count < 100)) && break
    offset=$((offset + 100))
  done
  jq -s 'unique_by(.id)' "$workdir/pages.jsonl" >"$events_json"
fi
# Apply the same limits regardless of source: newest first, drop older than since, then cap.
jq --argjson since "$since_epoch" --argjson max "$max_events" --argjson meta "$meta_json" '
  def epoch: capture("^(?<b>[^.Z]+)(\\.(?<f>[0-9]+))?Z$")
    | (.b + "Z" | fromdateiso8601) + (("0." + (.f // "0")) | tonumber);
  unique_by(.id) | map(select(.type | IN("sandbox.lifecycle.created", "sandbox.lifecycle.paused", "sandbox.lifecycle.resumed", "sandbox.lifecycle.killed")))
  | sort_by(.timestamp) | reverse
  | if $since == null then . else map(select((.timestamp | epoch) >= $since)) end
  | {cap_reached: ($max > 0 and length >= $max), events: (if $max > 0 then .[:$max] else . end)}
  | .events |= map(select((.eventData.sandbox_metadata // {}) as $m
      | all($meta | to_entries[]; $m[.key] == .value)))
' "$events_json" >"$workdir/limited.json"
cap_reached="$(jq -r '.cap_reached' "$workdir/limited.json")"
jq '.events' "$workdir/limited.json" >"$events_json"
if [[ -n "$save_events" ]]; then
  private_copy "$events_json" "$save_events"
fi
events_used="$(jq 'length' "$events_json")"
oldest_used="$(jq -r 'if length == 0 then "" else .[-1].timestamp end' "$events_json")"

# ------------------------------------------------------------------ current paused listing
listing_json="$workdir/paused.json"
if [[ "$skip_listing" == true ]]; then
  echo '[]' >"$listing_json"
else
  echo "Listing currently paused sandboxes..." >&2
  list_args=(sandbox list --state paused --format json --limit 0 --order asc)
  [[ -n "$metadata" ]] && list_args+=(--metadata "$metadata")
  e2b "${list_args[@]}" >"$listing_json"
fi

# ------------------------------------------------------------------ analysis (jq)
summary="$(jq -n \
  --slurpfile events "$events_json" --slurpfile paused "$listing_json" \
  --argjson windows "$windows_json" --argjson meta "$meta_json" \
  --argjson now "$now_epoch" --argjson since "$since_epoch" --argjson listing_skipped "$skip_listing" '
  def epoch: capture("^(?<b>[^.Z]+)(\\.(?<f>[0-9]+))?Z$")
    | (.b + "Z" | fromdateiso8601) + (("0." + (.f // "0")) | tonumber);
  def pctl(p): if length == 0 then null else
    sort as $s | ((($s | length) - 1) * p) as $k | ($k | floor) as $lo
    | ([$lo + 1, ($s | length) - 1] | min) as $hi
    | $s[$lo] + ($s[$hi] - $s[$lo]) * ($k - $lo) end;
  def meta_ok: if ($meta | length) == 0 then true else
    (.eventData.sandbox_metadata // {}) as $m | all($meta | to_entries[]; $m[.key] == .value) end;

  ($events[0]
    | map(select(meta_ok))
    | map(.t = (.timestamp | epoch))
    | map(select($since == null or .t >= $since))) as $evs
  | ($evs | group_by(.sandboxId) | map({
      sid: .[0].sandboxId,
      created: (map(select(.type == "sandbox.lifecycle.created") | .t) | min),
      killed: (map(select(.type == "sandbox.lifecycle.killed") | .t) | max),
      reason: (map(select(.type == "sandbox.lifecycle.killed") | .eventData.kill_reason // "unknown") | last),
      metadata: (map(.eventData.sandbox_metadata // empty) | last // {}),
      events: (map({t, k: .type}) | sort_by(.t, .k))
    })) as $tls
  | ([$tls[] | .sid as $sid | .reason as $reason | .events as $ev
      | range(0; $ev | length) as $i
      | select($ev[$i].k == "sandbox.lifecycle.paused")
      | ($ev[$i + 1] // {}) as $next
      | (($i > 0 and $ev[$i - 1].k == "sandbox.lifecycle.paused") or $next.k == "sandbox.lifecycle.paused" or $next.k == "sandbox.lifecycle.created") as $excluded
      | { sid: $sid, paused: $ev[$i].t,
          resumed: (if $next.k == "sandbox.lifecycle.resumed" and ($excluded | not) then $next.t else null end),
          excluded: $excluded,
          ended: (if $next.k == "sandbox.lifecycle.killed" then $next.t else null end),
          reason: (if $next.k == "sandbox.lifecycle.killed" then $reason else null end),
          until: ($next.t // $now) }
      | .gap = (if .resumed then .resumed - .paused else null end)
      | .observed = (.until - .paused)]) as $all_eps
  | ($all_eps | map(select(.excluded | not))) as $eps
  | ($eps | map(select(.resumed))) as $res
  | ($res | group_by(.sid) | map({key: .[0].sid, value: .}) | from_entries) as $res_by_sid
  | ($eps | map(.sid) | unique) as $paused_sids
  | ($paused[0] | map(.age = ($now - (.endAt | epoch)))) as $pn
  | {
      events_min: ($evs | map(.t) | min | if . == null then null else floor end), events_max: ($evs | map(.t) | max | if . == null then null else floor end),
      n_sandboxes: ($tls | length), n_episodes: ($eps | length), n_paused_now: ($pn | length),
      n_resumed_eps: ($res | length), n_paused_sids: ($paused_sids | length),
      n_ever_resumed: ($res | map(.sid) | unique | length),
      n_excluded: ($all_eps | map(select(.excluded)) | length),
      n_unpaired: (($evs | map(select(.type == "sandbox.lifecycle.resumed")) | length) - ($res | length)),
      gaps: ($res | map(.gap) | {p50: pctl(0.5), p90: pctl(0.9), p99: pctl(0.99), max: max}),
      reuse: [$windows[] | .seconds as $w | {label, returned_after: ($res | map(select(.gap > $w) | .sid) | unique | length), total: ($paused_sids | length), returning: ($res | map(.sid) | unique | length)}],
      creation: (($paused_sids | map({key: ., value: true}) | from_entries) as $pmap
        | [$windows[] | .seconds as $w
          | ($tls | map(select(.created != null and $pmap[.sid] == true and ((.killed // $now) - .created) >= $w))) as $obs
          | {label, observed: ($obs | length),
             returned: ($obs | map(select(.sid as $sid | .created as $c | any(($res_by_sid[$sid] // [])[]; (.resumed - $c) > $w))) | length)}]),
      episode_windows: [$windows[] | .seconds as $w
        | ($eps | map(if .resumed and .gap <= $w then "resumed" elif .observed >= $w then "not_resumed" else "unknown" end)) as $o
        | {label, resumed: ($o | map(select(. == "resumed")) | length), not_resumed: ($o | map(select(. == "not_resumed")) | length),
           unknown: ($o | map(select(. == "unknown")) | length), resumed_after: ($res | map(select(.gap > $w)) | length), n_res: ($res | length)}
        | .evaluable = .resumed + .not_resumed],
      never: (($pn | map({key: .sandboxId, value: true}) | from_entries) as $listed
        | ($listing_skipped | not) as $have_listing
        | def nlabel: if .ended then "observed kill: " + .reason
            elif ($have_listing | not) then "no later event (listing skipped; current state unconfirmed)"
            elif $listed[.sid] == true then "no later event, still in paused listing"
            else "no later event, not in paused listing (current state unconfirmed)" end;
        $eps | map(select(.resumed == null)) | group_by(nlabel)
        | map({label: (.[0] | nlabel), count: length}) | sort_by(-.count, .label)),
      never_ages: ($eps | map(select(.resumed == null and .ended)) | map(.ended - .paused) | if length == 0 then null else {p50: pctl(0.5), p90: pctl(0.9), max: max} end),
      buckets: (([$windows[] | .seconds]) as $ws
        | [range(0; $ws | length) as $i
            | {label: ("< " + $windows[$i].label),
               count: ($pn | map(select(.age < $ws[$i] and ($i == 0 or .age >= $ws[$i - 1]))) | length)}]
          + [{label: (">= " + $windows[-1].label), count: ($pn | map(select(.age >= $ws[-1])) | length)}]),
      paused_ages: [$windows[] | {label, count: (.seconds as $w | $pn | map(select(.age >= $w)) | length)}],
      tls: $tls, eps: $eps, excluded_eps: ($all_eps | map(select(.excluded))), pn: ($paused[0] | map({sandboxId, startedAt, endAt, metadata: (.metadata // {})}))
    }')"

# ------------------------------------------------------------------ report (bash printf)
fmt_td() { awk -v s="$1" 'BEGIN { if (s == "" || s == "null") { printf "n/a"; exit }
  if (s < 3600) printf "%dm", s / 60; else if (s < 86400) printf "%.1fh", s / 3600; else printf "%.1fd", s / 86400 }'; }
pct() { awk -v n="$1" -v d="$2" 'BEGIN { if (d == 0) printf "   n/a"; else printf "%5.1f%%", 100 * n / d }'; }
g() { jq -r "$1" <<<"$summary"; }
date_of() { date -u -r "$1" +%Y-%m-%d 2>/dev/null || date -u -d "@$1" +%Y-%m-%d; }

echo "========================================================================"
echo "SANDBOX PAUSE / RESUME ANALYSIS"
echo "========================================================================"
emin="$(g .events_min)"; emax="$(g .events_max)"
if [[ "$emin" != null ]]; then
  echo "Events window : $(date_of "$emin") .. $(date_of "$emax") ($(( (now_epoch - emin) / 86400 )) days)"
fi
echo "Sandboxes seen: $(g .n_sandboxes)   pause episodes: $(g .n_episodes)   currently paused: $(g .n_paused_now)"
requested=""
if [[ "$since_epoch" == null && "$max_events" == 0 ]]; then
  requested="all available history"
else
  [[ "$since_epoch" != null ]] && requested="last $(awk -v d="$since_days" 'BEGIN { printf "%g", d }') days"
  if ((max_events > 0)); then
    requested="${requested:+$requested, }newest $max_events events"
  fi
fi
if [[ "$using_defaults" == true ]]; then
  echo "History requested : $requested (DEFAULTS)"
  echo "DEFAULTS IN EFFECT: quick look only. For a retention decision run with"
  echo "  --since-days 30 --max-events 0 (or --all)."
else
  echo "History requested : $requested"
fi
if ((events_used > 0)); then
  oldest_epoch="$(jq -rn --arg t "$oldest_used" '$t | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601')"
  echo "Events used       : $events_used (oldest $(date -u -r "$oldest_epoch" +%Y-%m-%d\ %H:%M 2>/dev/null || date -u -d "@$oldest_epoch" +%Y-%m-%d\ %H:%M)Z)"
else
  echo "Events used       : 0"
fi
if [[ "$cap_reached" == true ]]; then
  echo "Note: the event cap was reached, so older events were left out. Raise --max-events or use --all."
fi
max_window_s="$(jq -r '.[-1].seconds' <<<"$windows_json")"
max_window_label="$(jq -r '.[-1].label' <<<"$windows_json")"
if [[ "$emin" != null ]] && (( now_epoch - emin < max_window_s )); then
  echo "Note: history spans $(fmt_td "$((now_epoch - emin))"), shorter than the $max_window_label window;"
  echo "      outcomes for that window will be mostly 'unknown'. Widen --since-days or use --all."
fi
echo
n_eps="$(g .n_episodes)"; n_res="$(g .n_resumed_eps)"; n_psids="$(g .n_paused_sids)"; n_ever="$(g .n_ever_resumed)"
echo "Pause episodes with observed resumes : $n_res of $n_eps ($(pct "$n_res" "$n_eps"))"
echo "Sandboxes with observed returns      : $n_ever of $n_psids ($(pct "$n_ever" "$n_psids"))"
echo "Ambiguous pause episodes excluded    : $(g .n_excluded)"
echo "Resume events without a usable pair  : $(g .n_unpaired)"
if ((n_res > 0)); then
  echo "Pause -> resume gap                   : p50 $(fmt_td "$(g .gaps.p50)"), p90 $(fmt_td "$(g .gaps.p90)"), p99 $(fmt_td "$(g .gaps.p99)"), max $(fmt_td "$(g .gaps.max)")"
fi
echo
echo "METHOD (deliberate choices)"
echo "  1. A return is a paused event immediately followed by a resumed event for the same"
echo "     sandbox, both inside the selected history. Idle time = resumed - paused."
echo "  2. Resumes with no such pause are counted as 'unpaired' and left OUT of the gap"
echo "     figures. The pause may predate the cutoff, an event may be missing, or the sequence"
echo "     may be ambiguous. The bounds below cover unknown gaps among recorded resumes only."
echo "  3. Consecutive pauses are ambiguous (e.g. missing or duplicate events) and excluded."
echo "  4. No later event does not establish current state. A paused-listing match confirms"
echo "     it is currently paused; absence could mean deletion or an unobserved resume."
echo "  5. Anything the current retention already deleted cannot show a resume, so rows at or"
echo "     beyond the current retention are not informative."
echo "  6. Counts are sandboxes and pause episodes, not users. Auto-resumes are included."

echo
echo "OBSERVED SANDBOX RETURNS AFTER N"
echo "(paused = sandboxes with at least one non-ambiguous pause episode in the available history;"
echo " returning = those with at least one observed resume)"
printf '%8s | %15s | %11s | %14s | %6s | %9s\n' "window" "resumed after N" "% of paused" "% of returning" "paused" "returning"
printf '%s\n' "--------------------------------------------------------------------------------"
while IFS=$'\t' read -r label returned_after total returning; do
  printf '%8s | %15s | %11s | %14s | %6s | %9s\n' "$label" "$returned_after" "$(pct "$returned_after" "$total")" "$(pct "$returned_after" "$returning")" "$total" "$returning"
done < <(g '.reuse[] | [.label, .returned_after, .total, .returning] | @tsv')
echo
echo "  Counts describe sandboxes, not unique users. Only observed pause/resume pairs count."
echo "  Missing history and insufficient follow-up can hide returns; no observed return"
echo "  does not mean a user will never come back."

echo
echo "OBSERVED PAUSE / RESUME TIMING"
echo "(evaluable = resumed within the window, or observed for at least its full duration)"
printf '%8s | %16s | %17s | %9s | %7s\n' "window" "resumed within" "not within window" "evaluable" "unknown"
printf '%s\n' "------------------------------------------------------------------------"
while IFS=$'\t' read -r label resumed not_resumed evaluable unknown; do
  printf '%8s | %7s %8s | %17s | %9s | %7s\n' "$label" "$resumed" "$(pct "$resumed" "$evaluable")" "$not_resumed" "$evaluable" "$unknown"
done < <(g '.episode_windows[] | [.label, .resumed, .not_resumed, .evaluable, .unknown] | @tsv')
echo
echo "  resumed within : share of evaluable pause episodes that were resumed inside the window"
echo "  unknown        : insufficient follow-up to classify a pause for this window"

echo
echo "RESUMES THAT CAME AFTER N DAYS IDLE, AS A SHARE OF RECORDED RESUMES"
echo "(observed = paired only; bounds = none or all of the unpaired recorded resumes were late)"
printf '%8s | %24s | %24s | %24s\n' "window" "observed (paired only)" "lower bound" "upper bound"
printf '%s\n' "-----------------------------------------------------------------------------------------"
n_unp="$(g .n_unpaired)"
while IFS=$'\t' read -r label resumed_after nres; do
  printf '%8s | %6s of %-5s %7s | %6s of %-5s %7s | %6s of %-5s %7s\n' "$label" "$resumed_after" "$nres" "$(pct "$resumed_after" "$nres")" "$resumed_after" "$((nres + n_unp))" "$(pct "$resumed_after" "$((nres + n_unp))")" "$((resumed_after + n_unp))" "$((nres + n_unp))" "$(pct "$((resumed_after + n_unp))" "$((nres + n_unp))")"
done < <(g '.episode_windows[] | [.label, .resumed_after, .n_res] | @tsv')
echo
echo "  Bounds assume measured pairs are accurate; they do not cover missing resume events."

echo
echo "OBSERVED SANDBOX RETURNS MEASURED FROM CREATION"
echo "(sandboxes with a created event and a pause; only those observed for at least N after"
echo " creation count. 'resumed after N' = any resume more than N after the sandbox was created)"
printf '%8s | %34s | %18s\n' "window" "resumed more than N after creation" "observed sandboxes"
printf '%s\n' "--------------------------------------------------------------------"
while IFS=$'\t' read -r label returned observed; do
  printf '%8s | %12s %9s             | %18s\n' "$label" "$returned" "$(pct "$returned" "$observed")" "$observed"
done < <(g '.creation[] | [.label, .returned, .observed] | @tsv')
echo
echo "  The two tables measure different clocks. 'Returns after N' above counts idle time since"
echo "  the last pause, which is what a user experiences. This table counts time since creation,"
echo "  which matches a rule keyed to the create timestamp. A sandbox created 10 days ago, paused"
echo "  yesterday and resumed today is a return after 7d here but not in the table above."

echo
echo "PAUSE EPISODES WITH NO OBSERVED RESUME"
while IFS=$'\t' read -r label count; do
  printf '  %-70s %6s\n' "$label" "$count"
done < <(g '.never[] | [.label, .count] | @tsv')
if [[ "$(g .never_ages)" != null ]]; then
  echo "  pause duration before observed kill: p50 $(fmt_td "$(g .never_ages.p50)"), p90 $(fmt_td "$(g .never_ages.p90)"), max $(fmt_td "$(g .never_ages.max)")"
fi

echo
echo "CURRENTLY PAUSED SANDBOXES BY TIME SINCE PAUSE"
while IFS=$'\t' read -r label count; do
  printf '  %-8s %6s\n' "$label" "$count"
done < <(g '.buckets[] | [.label, .count] | @tsv')
n_pn="$(g .n_paused_now)"
while IFS=$'\t' read -r label count; do
  echo "  $count of $n_pn sandboxes have been paused for at least $label"
done < <(g '.paused_ages[] | [.label, .count] | @tsv')
echo "========================================================================"

# ------------------------------------------------------------------ CSV
if [[ -n "$csv_path" ]]; then
  {
    echo "sandbox_id,created_at,first_paused_at,last_paused_at,pauses,resumes,last_resumed_at,killed_at,kill_reason,currently_paused,current_started_at,current_paused_at,metadata,excluded_pause_episodes"
    jq -r '
      def iso: if . == null then "" else todate end;
      (.excluded_eps | group_by(.sid) | map({key: .[0].sid, value: length}) | from_entries) as $excluded
      | (.eps | group_by(.sid) | map({key: .[0].sid, value: .}) | from_entries) as $by
      | (.pn | map({key: .sandboxId, value: .}) | from_entries) as $listing
      | (.tls | map({key: .sid, value: .}) | from_entries) as $tlmap
      | ((.tls | map(.sid)) + (.pn | map(.sandboxId)) | unique)[]
      | . as $sid | ($tlmap[$sid] // {sid: $sid, created: null, killed: null, reason: null, metadata: {}, events: []})
      | ($by[$sid] // []) as $e | ($listing[$sid]) as $cur
      | .metadata = (if (.metadata | length) > 0 then .metadata else ($cur.metadata // {}) end)
      | [ .sid, (.created | iso),
          ($e | map(.paused) | min | iso), ($e | map(.paused) | max | iso),
          ($e | length), ($e | map(select(.resumed)) | length),
          ($e | map(.resumed // empty) | max | iso),
          (.killed | iso), (if .killed then (.reason // "") else "" end),
          (if $cur then "yes" else "no" end),
          ($cur.startedAt // ""), ($cur.endAt // ""),
          (if (.metadata | length) > 0 then (.metadata | tojson) else "" end), ($excluded[$sid] // 0) ]
      | @csv' <<<"$summary"
  } >"$workdir/export.csv"
  private_copy "$workdir/export.csv" "$csv_path"
  echo "Per-sandbox detail written to $csv_path"
fi
