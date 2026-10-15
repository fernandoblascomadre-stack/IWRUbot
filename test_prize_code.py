"""Parity with iwillrugu's netlify/functions/_catch-prize-code.mjs (vectors printed by the Node side).
Run: python test_prize_code.py"""
import prize_code as pc

S = "bot-parity-secret"
fails = 0


def check(label, ok):
    global fails
    print(("  ok  " if ok else "FAIL  ") + label)
    fails += 0 if ok else 1


code = pc.format_code(S, "2ABCDEFGHJ")
check("code matches the server's formatPrizeCode", code == "IWRU-2ABCD-EFGHJ-XT05H-Y893V")
check("registration signature matches the server's", pc.registration_signature(S, "1700000000000", [code, "IWRU-1ZZZZ-ZZZZZ-299VX-YC1H0"])
      == "44ec483406a71aaf216c2498097cc40e4e3a7e13d97daa4f64ef6cf022de72a3")
check("verifies() accepts its own code", pc.verifies(S, code))
check("verifies() accepts lower case / spaces", pc.verifies(S, code.lower().replace("-", " ")))
check("verifies() refuses another secret (the 2026-10-12 mask bug)", not pc.verifies("****************ab12", code))
check("verifies() refuses a changed rarity digit", not pc.verifies(S, code.replace("IWRU-2", "IWRU-4")))
check("check codes start with the never-redeemable digit 0", pc.check_code(S).startswith("IWRU-0") and pc.verifies(S, pc.check_code(S)))
check("new codes are unique", len({pc.new_code(S, "common") for _ in range(2000)}) == 2000)
print("\nOK" if not fails else f"\n{fails} failed")
raise SystemExit(1 if fails else 0)
