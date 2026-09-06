"""Cars shapes (PLAN P10.4, SPEC §5 *Cars*, RECON §11c, §13c-ii). Pure.

On cars a `null` means "CR has no value", never "you cannot see it": the API never withholds, so
`scores_available` reports `available` or `absent` and never `unavailable`.
"""

from __future__ import annotations

from typing import Any

SUMMARY_SCORE_KEYS = ("safety_verdict", "popular_score", "fuel_economy")
STANDARD_SCORE_KEYS = (
    "overall_score",
    "road_test_score",
    "predicted_reliability",
    "owner_satisfaction",
    "recommended_flag",
)


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return v


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def y_n(value: Any, warnings: list[str] | None = None, field: str = "isRecommended") -> bool | None:
    """Cars booleans are the strings "Y"/"N" (RECON §11c) — never "Yes"/"No", never bools."""
    if value == "Y":
        return True
    if value == "N":
        return False
    if value is None:
        return None
    if warnings is not None:
        w = f"coercion_failed:{field}"
        if w not in warnings:
            warnings.append(w)
    return None


def _fuel(spec: Any) -> dict | None:
    if not isinstance(spec, dict):
        return None
    return {
        "annual_fuel_cost_dollar": _num(spec.get("annualFuelCostDollar")),
        "annual_fuel_consumption_gal": _num(spec.get("annualFuelConsumptionGal")),
        "cruise_range_miles": _num(spec.get("cruiseRangeMiles")),
    }


def _incentive(inc: Any) -> dict | None:
    if not isinstance(inc, dict):
        return None
    return {
        "cash_value_up_to": _num(inc.get("cashValueUpTo")),
        "effective_date": inc.get("effectiveDate"),
        "expiry_date": inc.get("expiryDate"),
    }


def listing_shape(entry: dict) -> dict:
    """A `v2/cr/cars` entry → the summary row. Road-test keys are OMITTED, not nulled (D13)."""
    return {
        "model_year_id": _int(entry.get("modelYearId")),
        "make": entry.get("makeName"),
        "slug_make": entry.get("slugMakeName"),
        "model": entry.get("modelName"),
        "slug_model": entry.get("slugModelName"),
        "year": _int(entry.get("modelYear")),
        # one shape on every cars tool: a LIST, like `cr_car_search` and `cr_car` (SPEC §5)
        "states": [entry["modelYearStateName"]] if entry.get("modelYearStateName") else [],
        "car_id": _int(entry.get("carId")),
        "version": entry.get("carVersionName"),
        "powertrain": entry.get("powerTrainType"),
        "test_state": entry.get("testStateName"),
        "generation": {
            "id": _int(entry.get("modelGenerationId")),
            "start_year": _int(entry.get("modelGenerationStartYear")),
        },
        "safety_verdict": _num(entry.get("safetyVerdictRatingScore")),
        "popular_score": _num(entry.get("crPopularScore")),
        "fuel_economy": _fuel(entry.get("fuelEconomySpecs")),
        "incentive": _incentive(entry.get("incentive")),
        "ratings_copied_from": (
            {
                "year": _int(entry.get("ratingsCopiedFromCarModelYear")),
                "version": entry.get("ratingsCopiedFromCarVersionName"),
            }
            if entry.get("ratingsCopiedFromCarId") is not None
            else None
        ),
    }


def _default_car(model_year: dict) -> dict:
    cars = model_year.get("cars") or []
    for c in cars:
        if isinstance(c, dict) and c.get("isDefault") == "Y":
            return c
    return cars[0] if cars and isinstance(cars[0], dict) else {}


def car_shape(payload: dict, detail: str, warnings: list[str] | None = None) -> dict:
    """`v2/cr/modelYears/{id}` → the standard (or full) car record. Nulls are CR absences."""
    resp = payload.get("response") if isinstance(payload, dict) else None
    resp = resp if isinstance(resp, dict) else {}
    my = resp.get("modelYear") if isinstance(resp.get("modelYear"), dict) else {}
    make = resp.get("make") if isinstance(resp.get("make"), dict) else {}
    model = resp.get("model") if isinstance(resp.get("model"), dict) else {}
    gen = resp.get("modelGeneration") if isinstance(resp.get("modelGeneration"), dict) else {}
    car = _default_car(my)
    tr = car.get("testRatings") if isinstance(car.get("testRatings"), dict) else {}
    rc = car.get("ratingsCategory") if isinstance(car.get("ratingsCategory"), dict) else {}
    rel = (
        model.get("reliabilityRatings") if isinstance(model.get("reliabilityRatings"), dict) else {}
    )
    own = (
        model.get("ownerSatisfactionRatings")
        if isinstance(model.get("ownerSatisfactionRatings"), dict)
        else {}
    )
    price = my.get("price") if isinstance(my.get("price"), dict) else {}
    states = [
        s.get("modelYearStateName")
        for s in my.get("modelYearStates") or []
        if isinstance(s, dict) and s.get("modelYearStateName")
    ]
    shape: dict[str, Any] = {
        "model_year_id": _int(my.get("modelYearId")),
        "make": make.get("makeName"),
        "slug_make": make.get("slugMakeName"),
        "model": model.get("modelName"),
        "slug_model": model.get("slugModelName"),
        "year": _int(my.get("modelYear")),
        "states": states,  # always a list — never a string for one, null for none
        "car_id": _int(car.get("carId")),
        "version": car.get("carVersionName"),
        "test_state": (car.get("testState") or {}).get("testStateName"),
        "car_type": (
            {"id": _int(rc.get("carTypeId")), "name": rc.get("carTypeDisplayName")}
            if rc.get("carTypeId") is not None
            else None
        ),
        "category": (
            {"id": _int(rc.get("categoryId")), "name": rc.get("categoryPluralName")}
            if rc.get("categoryId") is not None
            else None
        ),
        "overall_score": _num(tr.get("overallTestScore")),
        "road_test_score": _num(tr.get("roadTestScore")),
        "rank": _int(rc.get("overallRank")),
        "score_range": (
            {"min": _num(rc.get("overallTestScoreMin")), "max": _num(rc.get("overallTestScoreMax"))}
            if rc.get("overallTestScoreMax") is not None
            or rc.get("overallTestScoreMin") is not None
            else None
        ),
        "sort_index": _num(car.get("overallScoreSortIndex")),
        "recommended": y_n((my.get("expertRatings") or {}).get("isRecommended"), warnings),
        "predicted_reliability": _num(rel.get("predictedReliabilityScore")),
        "predicted_reliability_rating": _num(rel.get("predictedReliabilityRating")),
        "owner_satisfaction": _num(own.get("predictedWouldBuyAgainRating")),
        "owner_satisfaction_percent": _num(own.get("predictedWouldBuyAgainPercent")),
        "popular_score": _num(my.get("crPopularScore")),
        "safety_verdict": _num((tr.get("safetyVerdict") or {}).get("ratingScore")),
        "price": {
            "msrp_min": _num(price.get("defaultMsrpMin")),
            "msrp_max": _num(price.get("defaultMsrpMax")),
            "retail_avg_min": _num(price.get("currentRetailAvgValueMin")),
            "retail_avg_max": _num(price.get("currentRetailAvgValueMax")),
        }
        if price
        else None,
        "fuel_economy": _fuel(car.get("fuelEconomySpecs")),
        "generation": {
            "id": _int(gen.get("modelGenerationId")),
            "start_year": _int(gen.get("modelGenerationStartYear")),
        },
    }
    if detail == "full":
        shape["ratings"] = [
            {
                "id": _int(r.get("testRatingId")),
                "name": r.get("testRatingName"),
                "composite_score": _num(r.get("compositeRatingScore")),
                "tests": [
                    {
                        "id": _int(t.get("testId")),
                        "name": t.get("testName"),
                        "value": _num(t.get("testRatingValue")),
                    }
                    for t in r.get("tests") or []
                    if isinstance(t, dict)
                ],
            }
            for r in tr.get("ratings") or []
            if isinstance(r, dict)
        ]
        shape["feature_group_ratings"] = [
            {
                "id": _int(g.get("featureGroupId")),
                "name": g.get("featureGroupName"),
                "score": _num(g.get("featureGroupRatingScore")),
                "value": _num(g.get("featureGroupRatingValue")),
            }
            for g in tr.get("featureGroupRatings") or []
            if isinstance(g, dict)
        ]
        shape["safety_verdict_detail"] = (
            tr.get("safetyVerdict") if isinstance(tr.get("safetyVerdict"), dict) else None
        )
        crash = my.get("crashTestRatings")
        shape["crash_tests"] = (
            crash[0]
            if isinstance(crash, list) and crash and isinstance(crash[0], dict)
            else (
                car.get("crashTestRatings")
                if isinstance(car.get("crashTestRatings"), dict)
                else None
            )
        )
        specs = my.get("modelYearSpecs") if isinstance(my.get("modelYearSpecs"), dict) else {}
        shape["specs"] = {
            "body_styles": specs.get("bodyStyles"),
            "drive_wheels": specs.get("driveWheels"),
            "engines": specs.get("engines"),
            "seating": specs.get("seating"),
            "transmissions": specs.get("transmissions"),
            "trims": specs.get("trims"),
            "car_specs": car.get("carSpecs"),
        }
        warranty = my.get("warranty") if isinstance(my.get("warranty"), dict) else {}
        shape["warranty"] = warranty.get("leastNonZeroInfo")
        shape["incentive"] = _incentive(my.get("incentive"))
        reviews = my.get("expertReviews") if isinstance(my.get("expertReviews"), dict) else {}
        shape["review_summary"] = reviews.get("modelYearSummaryLong")
        shape["reliability_text"] = rel.get("predictedReliabilityText")
    return shape


def car_scores_available(rows: list[dict], keys: tuple[str, ...]) -> dict[str, str]:
    """`available` if any row carries the value, else `absent` — never `unavailable`."""
    out: dict[str, str] = {}
    for key in keys:
        field = "recommended" if key == "recommended_flag" else key
        found = False
        for r in rows:
            v = r.get(field)
            if isinstance(v, dict):
                v = any(x is not None for x in v.values())
                if v:
                    found = True
                    break
            elif v is not None:
                found = True
                break
        out[key] = "available" if found else "absent"
    return out
