
from core.multi_timeframe_smc import analyze_multi_timeframe_smc
from core.zone_validator import validate_smc_zones

symbol = "USDJPY"

mtf = analyze_multi_timeframe_smc(symbol, bars=500)

print("\n========== STRUCTURE EVENTS ==========")

for name, tf in mtf.get("timeframes", {}).items():
    print(
        name,
        "trend:", tf.get("trend"),
        "event:", tf.get("event"),
        "direction:", tf.get("event_direction"),
        "age:", tf.get("event_age_bars")
    )

print("\n========== ZONE VALIDATION ==========")

if mtf.get("valid"):
    zones = validate_smc_zones(symbol, mtf)

    print("Zone result valid:", zones.get("valid"))
    print("Zone summary:", zones.get("summary"))

    print("\nEligible zones:")

    for zone in zones.get("eligible_zones", []):
        print({
            "timeframe": zone.get("timeframe"),
            "type": zone.get("type"),
            "direction": zone.get("direction"),
            "eligible": zone.get("eligible"),
            "zone_state": zone.get("zone_state"),
            "inside_zone": zone.get("inside_zone"),
            "first_touch_index": zone.get("first_touch_index"),
            "has_recorded_touch": zone.get("has_recorded_touch")
        })
else:
    print("MTF analysis failed:", mtf.get("reason"))
