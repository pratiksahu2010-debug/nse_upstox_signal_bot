"""Offline, bounded research over persisted signal outcomes. Never mutates live strategy."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from itertools import product
from statistics import mean

MIN_TRAIN_SAMPLES = 40
MIN_TEST_SAMPLES = 12
MAX_CANDIDATES = 12

def effective_label(row):
    # Human feedback is authoritative; automatic outcome remains separately visible.
    return row.get("user_label") or row.get("auto_label")

def _score(rows):
    values=[float(r["net_r"]) for r in rows if r.get("net_r") is not None]
    if not values: return None
    wins=sum(v>0 for v in values)
    return {"n":len(values),"win_rate_pct":round(100*wins/len(values),1),
            "mean_net_r":round(mean(values),4),"sum_net_r":round(sum(values),4)}

def walk_forward(rows, min_train=MIN_TRAIN_SAMPLES, min_test=MIN_TEST_SAMPLES):
    """Chronological 60/20/20 train/validation/test; candidate filters are bounded."""
    eligible=[r for r in rows if r.get("bar_ts") and r.get("net_r") is not None
              and r.get("snapshot") and effective_label(r) not in {"SKIP"}]
    eligible.sort(key=lambda r:r["bar_ts"])
    if len(eligible)<min_train+2*min_test:
        return {"status":"insufficient_data","samples":len(eligible),
                "required":min_train+2*min_test,"proposal":None}
    n=len(eligible); train_end=max(min_train,int(n*.6)); val_end=max(train_end+min_test,int(n*.8))
    train,validation,test=eligible[:train_end],eligible[train_end:val_end],eligible[val_end:]
    if len(validation)<min_test or len(test)<min_test:
        return {"status":"insufficient_data","samples":n,"required":min_train+2*min_test,"proposal":None}
    # Bounded cohort filters only. They do not generate hypothetical signals or change entry rules.
    candidates=[{"min_adx":a,"min_rvol":v} for a,v in product((22.0,24.0,26.0),(0.0,1.0,1.2))][:MAX_CANDIDATES]
    def subset(data,c):
        return [r for r in data
                if float(r["snapshot"].get("adx",0) or 0)>=c["min_adx"]
                and float(r["snapshot"].get("rvol",0) or 0)>=c["min_rvol"]]
    ranked=[]
    for c in candidates:
        tr,va,te=subset(train,c),subset(validation,c),subset(test,c)
        if len(tr)<min_train or len(va)<min_test or len(te)<min_test: continue
        a,b,d=_score(tr),_score(va),_score(te)
        # Validation chooses; held-out test is reported only after selection.
        ranked.append((b["mean_net_r"],c,a,b,d))
    if not ranked:
        return {"status":"no_candidate_met_sample_floor","samples":n,"proposal":None}
    ranked.sort(key=lambda x:(x[0],x[1]["min_adx"],x[1]["min_rvol"]),reverse=True)
    _,best,tr,va,te=ranked[0]
    baseline=_score(test)
    # Require a positive held-out mean and improvement over the unfiltered cohort.
    if not baseline or te["mean_net_r"]<=0 or te["mean_net_r"]<=baseline["mean_net_r"]:
        return {"status":"no_out_of_sample_improvement","samples":n,
                "baseline_test":baseline,"candidate_test":te,"proposal":None}
    return {"status":"proposal_ready","samples":n,"train":tr,"validation":va,
            "held_out_test":te,"baseline_test":baseline,
            "proposal":{"scope":"research_filter_only","diff":{
                "research_filters.min_adx":best["min_adx"],
                "research_filters.min_rvol":best["min_rvol"]},
                "note":"Post-signal cohort filter research only; not a replacement for or modification to live entry rules."}}

def build_proposal(rows, now=None):
    now=now or datetime.now().astimezone()
    report=walk_forward(rows)
    if report.get("proposal"):
        report["proposal"]["created_at"]=now.isoformat()
        report["proposal"]["id"]=now.strftime("%Y%m%d%H%M%S")
        report["proposal"]["state"]="PENDING"
    return report
