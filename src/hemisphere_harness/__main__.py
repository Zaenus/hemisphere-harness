"""`python -m hemisphere_harness` lists the providers available in this environment."""

from .providers import left_providers, right_providers


def main() -> None:
    for registry in (right_providers, left_providers):
        rows = registry.describe()
        width = max(len(label) for label, _ in rows)
        print(f"{registry.kind.capitalize()} hemisphere providers  ({registry.variable})")
        for label, summary in rows:
            print(f"  {label:<{width}}  {summary}")
        print()
    print("To add a provider, see docs/providers.md.")


if __name__ == "__main__":
    main()
