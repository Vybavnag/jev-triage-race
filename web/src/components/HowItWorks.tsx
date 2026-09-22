import type { AppConfig } from "../types";

export function HowItWorks({ config }: { config: AppConfig }) {
  return (
    <section className="prose">
      <p>
        Both sides answer the same questions about the same text. The only
        difference is what kind of model does the answering. The bundled
        tickets carry expected answers written by hand, and the results table
        shows those beside each side's answer. Nothing here decides who was
        right; you read across the row.
      </p>

      <h3>What each side does</h3>
      <p>
        Jev is a classifier, not a text generator. One request carries the text
        plus every question, and they are evaluated in parallel: a yes/no comes
        back as a probability, a pick-one as a probability per option, and a
        scale as a score across its levels. What comes back is a distribution
        you can branch on directly.
      </p>
      <p>
        The LLM answers the same questions in one call using structured output,
        so it returns a typed object rather than prose. That keeps the
        comparison fair: one request per text on each side, no retry loops, no
        multi-step prompting.
      </p>

      <h3>Racing your own tickets</h3>
      <p>
        Paste support messages, one per paragraph, and they run through the same
        race as the bundled set. They have no expected answers, so the table
        shows both sides' answers and times and you compare them. Your text is
        never sent back in the event stream; the page keeps its own copy.
      </p>

      <h3>Code review</h3>
      <p>
        Jev cannot write a review, so a review here is a checklist. You paste
        one block of code and a short list of plain yes/no questions, and both
        sides answer every one of them about that code, each in its own lane:
        a side's track fills the moment its answer lands. Jev returns a
        probability per question; the LLM returns a yes or no per question
        through structured output. The questions go into the prompt on both
        sides, never into the LLM's output shape, so changing a question never
        changes what the LLM is allowed to return. Nothing is graded: the table
        is the comparison, and rows where the two sides disagree are marked.
      </p>

      <h3>Reading the results</h3>
      <p>
        Jev's urgent answer is shown as yes when its probability is at least
        0.5, with the probability next to it. Its team pick carries the
        probability it gave that team. The LLM asserts one value per question
        with no number attached. The same 0.5 bar turns Jev's review
        probabilities into a yes or a no.
      </p>
      <p>
        Jev returns frustration as a probability-weighted position on a
        zero-indexed scale, so a five-level rubric produces a value between 0.0
        and 4.0. The app maps that to a level from 1 to 5. Calls that error out
        are counted separately and kept out of the latency average, so a rate
        limit never flatters a side. The only verdicts the app gives are the
        ones it can measure: faster and cheaper.
      </p>

      <h3>Keeping the race honest</h3>
      <p>
        The Claude side runs with effort set to low, which is the setting a
        classification workload would actually use, and thinking is left on.
        Automatic model fallbacks are off, so a refusal is recorded as an error
        rather than quietly answered by a different model. A reply that runs out
        of room or fails to parse is recorded as malformed, never as a guess.
        Costs come from published per-token prices: Jev bills ${config.jev.price_in}{" "}
        per million input tokens with output free, and each Claude model uses
        its own rate. When a provider does not report token usage, the cost
        reads as unknown instead of zero.
      </p>

      <h3>Your keys</h3>
      <p>
        Both sides run on your own account. The keys you enter stay in this
        tab and travel with each run to this server, which builds that run's
        provider clients from them, closes them when the run ends, and keeps
        nothing. They are never stored, logged, or echoed. Close the tab and
        they are gone; use the link at the top to change or forget them
        sooner.
      </p>

      <h3>Where the numbers come from</h3>
      <p>
        Latency is measured around each API call, so it includes network time
        from wherever this is running. The {config.dataset_size} bundled
        tickets are synthetic and hand-labeled, which makes them a reasonable
        smoke test and not a benchmark you should cite. Paste your own tickets,
        or your own code, to see the same comparison on something that matters
        to you.
      </p>
    </section>
  );
}
