import type { AppConfig } from "../types";

export function HowItWorks({ config }: { config: AppConfig }) {
  return (
    <section className="prose">
      <p>
        Both sides answer the same three questions about the same support
        message, and both are graded against labels shipped with the dataset.
        The only difference is what kind of model does the answering.
      </p>

      <h3>What each side does</h3>
      <p>
        Jev is a classifier, not a text generator. One request carries the
        message plus all three questions, and they are evaluated in parallel:
        is this urgent (a yes/no probability), which team should own it (a
        probability per option), and how frustrated is the customer (a score
        across five ordered levels). What comes back is a distribution you can
        branch on directly.
      </p>
      <p>
        The LLM answers the same three questions in one call using structured
        output, so it returns a typed object rather than prose. That keeps the
        comparison fair: one request per message on each side, no retry loops,
        no multi-step prompting.
      </p>

      <h3>How the scoring works</h3>
      <p>
        A ticket counts as correct on urgency when the probability is at least
        0.5 and matches the label. Team is the highest-probability option.
        Frustration counts as correct within one level, because neighbouring
        levels are genuinely arguable.
      </p>
      <p>
        Jev returns frustration as a probability-weighted position on a
        zero-indexed scale, so a five-level rubric produces a value between 0.0
        and 4.0. The app maps that to 1–5 before grading. Tickets that error out
        are counted separately and kept out of the accuracy denominator, so a
        rate limit never inflates a score.
      </p>

      <h3>Keeping the race honest</h3>
      <p>
        The Claude side runs with effort set to low, which is the setting a
        classification workload would actually use, and thinking is left on.
        Automatic model fallbacks are off, so a refusal is recorded as an error
        rather than quietly answered by a different model. Costs come from
        published per-token prices: Jev bills ${config.jev.price_in} per million
        input tokens with output free, and each Claude model uses its own rate.
        When a provider does not report token usage, the cost reads as unknown
        instead of zero.
      </p>

      <h3>Where the numbers come from</h3>
      <p>
        Latency is measured around each API call, so it includes network time
        from wherever this is running. Accuracy depends on the dataset: the{" "}
        {config.dataset_size} bundled tickets are synthetic and hand-labeled,
        which makes them a reasonable smoke test and not a benchmark you should
        cite. Swap in your own labeled data to get a number that means something
        for your workload.
      </p>
    </section>
  );
}
