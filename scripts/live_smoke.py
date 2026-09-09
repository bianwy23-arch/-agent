"""Small, real-provider integration check; not the planned 40-60 task eval."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from shopping_agent.config import Settings
from shopping_agent.runtime import ShoppingRuntime


async def main(args):
    runtime = ShoppingRuntime(Settings.load(args.root), runtime_dir=args.output)
    results = []
    async def run(cid, text, check):
        result = await runtime.run(cid, text)
        passed = result["runtime"]["error"] is None and bool(check(result))
        results.append({"input": text, "passed": passed, "result": result})
        print(json.dumps({"case":len(results),"passed":passed,"kind":result["kind"],"runtime":result["runtime"]},ensure_ascii=False),flush=True)
        return result
    try:
        cid = runtime.create_conversation()
        first = await run(cid, "找一个100美元以内的耳机，没有其他要求，推荐一个就好。",
                          lambda r:r["kind"]=="recommendation" and bool(r["product_ids"]) and r["state"]["requirements"]["budget"]["value"]["amount"]=="100")
        if first["product_ids"]:
            excluded = first["product_ids"][0]
            await run(cid,"预算提高到150美元，刚才推荐的第一个不要了。先只记录这些修改，不用再搜索或推荐。",
                      lambda r:r["state"]["requirements"]["budget"]["value"]["amount"]=="150" and excluded in r["state"]["excluded"])
            await run(cid,"只恢复预算，之前排除的那个仍然不要。先只告诉我当前预算，不用再推荐。",
                      lambda r:r["state"]["requirements"]["budget"]["value"]["amount"]=="100" and excluded in r["state"]["excluded"])
            await run(cid,"如果预算提高到200美元呢？先只记录临时方案，不用搜索，正式预算别改。",
                      lambda r:r["state"]["requirements"]["budget"]["value"]["amount"]=="100" and r["state"]["exploration"] is not None)
        await run(runtime.create_conversation(),"给我找一个100元人民币以内的耳机。",
                  lambda r:r["kind"] in {"needs_user","data_limited"} and not r["product_ids"] and r["state"]["requirements"]["budget"]["value"]["currency"]=="CNY")
    finally:
        report={"at":datetime.now(timezone.utc).isoformat(),"provider":"DeepSeek","model":runtime.settings.model,
                "scope":"five-turn integration smoke, not full agent eval", "passed":sum(r["passed"] for r in results),
                "executed":len(results),"cases":results}
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/"smoke-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        await runtime.close()


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=Path.cwd())
    p.add_argument("--output",type=Path,required=True)
    asyncio.run(main(p.parse_args()))
