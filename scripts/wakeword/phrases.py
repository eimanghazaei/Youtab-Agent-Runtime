"""The phrase inventory the "hey youtab" detector is trained and judged against.

One module, imported by every stage, so the training set and the evaluation
set can never drift apart on what counts as the wake word and what counts as a
near miss.

Three groups:

``POSITIVE_SPELLINGS``
    Orthographies that all phonemize to a real-world pronunciation of "hey
    youtab". espeak-ng flaps the /t/ in the single-word spelling
    (``hˈeɪ jˈuːɾæb``) and keeps a hard /t/ when the name is split
    (``hˈeɪ jˈuː tˈæb``). Both are things people actually say, so the model is
    trained on the mixture rather than on one arbitrary transcription.

``HARD_NEGATIVES``
    Phrases chosen to sit one or two phonemes away from the wake word. These
    are what a detector trained only against unrelated speech gets wrong, so
    they are synthesized from the same voices as the positives and mixed into
    the negative class. Note the deliberate absences: nothing in this list
    *contains* the wake phrase, because labelling an utterance that includes
    "hey youtab" as a negative would teach the model to suppress a real fire.
    The wake phrase is the carrier *and* the name, so the name on its own is
    not it — "youtab.", "open youtab." and "okay youtab." are all in the list
    and none of them breaches that rule. What may never appear in it is "hey"
    followed by the name.

``SOFT_NEGATIVES``
    Ordinary sentences. Cheap breadth against everyday speech, complementing
    the recorded human negatives from Speech Commands.
"""

from __future__ import annotations

#: Spellings that phonemize to a genuine pronunciation of the wake phrase.
#: The weights bias generation toward the compound spelling, which is how the
#: product name is written and how most speakers say it.
POSITIVE_SPELLINGS: tuple[tuple[str, int], ...] = (
    ("hey youtab.", 5),
    ("hey yoo tab.", 2),
    ("hey you tab.", 2),
    ("hey, youtab.", 1),
    ("hey youtab!", 1),
)

#: Near misses. A detector that fires on these is worse than useless: it wakes
#: the agent while its owner is talking about something else.
HARD_NEGATIVES: tuple[str, ...] = (
    # The name without the carrier word — "youtab" alone must not wake it.
    "youtab.",
    "youtab is running.",
    "open youtab.",
    # The carrier word without the name.
    "hey.",
    "hey there.",
    "hey, hold on.",
    # The name under the wrong carrier, and other products' wake words. Owner
    # decision: all three were once recorded as near phrases with their label
    # decided beside this contract rather than in it, and they are governed
    # taxonomy now, so the label is read off this tuple like every other
    # negative.
    #
    # "okay youtab." is our own name behind a carrier that is not "hey". Every
    # POSITIVE_SPELLINGS entry starts with "hey", and tools/wake_word.py's
    # docstring calls the configured wake_word.phrase "purely cosmetic; engine
    # keys detection" — so the product fires on whatever the positives trained
    # it on, carrier included. It is the phrase that separates a model keyed on
    # the whole phrase from one keyed on "youtab" alone, with "okay tab." below
    # as its control.
    #
    # "hey google." and "hey siri." are the carrier plus a *competing*
    # assistant's name: the same construction as "hey utah." and "hey nutmeg."
    # below, with the two best-known wrong names in it. Firing on either wakes
    # the agent while its owner is talking to a different device.
    "okay youtab.",
    "hey google.",
    "hey siri.",
    # One phoneme off in the stressed vowel or the coda.
    "hey youtube.",
    "hey you talk.",
    "hey you tap.",
    "hey you two.",
    "hey your tab.",
    "hey new tab.",
    "hey do tab.",
    "hey utah.",
    "hey yoda.",
    "hey yodel.",
    "hey nutmeg.",
    "hey cab.",
    "hey grab.",
    "hey lab it.",
    "hey stab.",
    "a new tab.",
    "you talk.",
    "the tab.",
    "close the tab.",
    "another tab.",
    "hey you.",
    "okay tab.",
    "they took a tab.",
    "hey mutable.",
    "hey suitable.",
    "hey rotate.",
    "hey you had.",
    "hey you app.",
    "hey youth.",
    # ---- /eɪ/ without the /h/ onset -------------------------------------
    # Measured, not guessed. Scoring the first trained model over held-out
    # RECORDED speech put "eight" at the very top of the tail: 0.9985 against
    # an operating threshold of 0.9991, so a single utterance of it was what
    # pinned the threshold that high, and that threshold is what cost half the
    # wake words. "eight" is /eɪt/ and "hey" is /heɪ/ — the model had learned
    # the diphthong and not the /h/ that must precede it.
    #
    # Everything in this block is that same vowel with no onset, so training
    # sees the contrast rather than inferring it. The bare digits and words
    # below are the other Speech Commands entries that scored highest.
    "eight.",
    "eighty.",
    "eighteen.",
    "eight tabs.",
    "ate a late lunch.",
    "wait.",
    "wait a moment.",
    "weight.",
    "great.",
    "the letter a.",
    "a tab.",
    "they take a tab.",
    "two.",
    "four.",
    "zero.",
    "house.",
    "visual.",
    "two tabs.",
    # ---- /h/ onset that is not "hey" ------------------------------------
    # Round 2 taught the model that the /eɪ/ diphthong needs an /h/ before it,
    # and "eight" left the tail completely. What moved into its place was
    # /h/-initial ordinary speech: "happy" scored 0.999995 on held-out recorded
    # audio, alongside "down", "stop", "yes", "left" and "no". Having learned
    # to require the onset, the model now over-trusts it.
    #
    # These give it /h/ followed by something that is emphatically not
    # "youtab", so the onset stops being sufficient on its own.
    "happy.",
    "happy to help.",
    "hello.",
    "hello there.",
    "how are you.",
    "how about it.",
    "hi.",
    "help.",
    "here.",
    "here you go.",
    "home.",
    "head.",
    "hand.",
    "hold on.",
    "hot.",
    # Measured alongside "happy" in the round-2 tail.
    "down.",
    "stop.",
    "yes.",
    "left.",
    "no.",
)

#: Ordinary speech. Breadth, not precision.
SOFT_NEGATIVES: tuple[str, ...] = (
    "what is the weather going to be like tomorrow afternoon.",
    "please open the second file in the project directory.",
    "i think we should refactor that module before the release.",
    "can you tell me how long the build usually takes.",
    "the meeting has been moved to three o'clock on thursday.",
    "there were about forty people waiting outside the hall.",
    "send the report to everyone on the distribution list.",
    "she said the train would arrive a little after seven.",
    "remind me to call the bank when the office opens.",
    "the documentation for that library is out of date.",
    "i left my keys somewhere in the living room.",
    "turn the volume down a little bit please.",
    "we need another two boxes for the rest of the books.",
    "the server stopped responding around midnight last night.",
    "he asked whether the invoice had already been paid.",
    "let me know if the tests are still failing after that.",
    "add a comment explaining why this branch exists.",
    "the flight was delayed by nearly three hours.",
    "put the groceries on the counter next to the sink.",
    "i cannot find the notes from yesterday's review.",
)


#: The near misses the trained detector was *measured* to confuse, in
#: descending order of how often they fired. This list is evidence, not
#: intuition: it comes from breaking the held-out false accepts down by phrase
#: after the first training run, and the ordering was not what guesswork
#: predicted. "hey youtube" — the confusion this phrase inventory was designed
#: around — fired on 1 clip in 106. What actually fires is minimal pairs on the
#: final syllable ("hey you tap", one voicing feature away) and the frame
#: "hey <something> tab".
#:
#: They are synthesized at a higher rate than the rest of HARD_NEGATIVES so
#: training sees the contrast often enough to learn it. Every one of them is
#: already in HARD_NEGATIVES; this is a weighting, not a new class.
#:
#: "okay youtab.", "hey google." and "hey siri." are deliberately *not* here,
#: although the Owner decision that put them in HARD_NEGATIVES could have put
#: them here too. Nothing has ever measured them: they were never synthesized
#: and have never been recorded, so an entry would be intuition dressed as
#: evidence — the one thing this list is defined not to be, and the lesson of
#: "hey youtube" ranking far below what guesswork predicted. The recording
#: round that collects them is what can earn them a place.
CONFUSABLE_NEGATIVES: tuple[str, ...] = (
    "hey you tap.",
    "hey your tab.",
    "hey new tab.",
    "hey utah.",
    "hey do tab.",
    "hey stab.",
    "youtab.",
    "hey there.",
    "youtab is running.",
    "hey you had.",
    "hey cab.",
    "hey you talk.",
    "a new tab.",
    # Round 2 evidence, from scoring the first model over held-out RECORDED
    # speech rather than over synthesized near misses. Ordered by the highest
    # score each word reached; only words that were actually measured appear
    # here, which is why the phonetic neighbours of "eight" ("wait", "great",
    # "eighty") sit in HARD_NEGATIVES and not in this weighted list.
    #
    #   eight   0.9985   <- pinned the operating threshold by itself
    #   two     0.9832
    #   visual  0.8568
    #   four    0.6853
    #   zero    0.5508
    #   house   0.5412
    "eight.",
    "two.",
    "visual.",
    "four.",
    "zero.",
    "house.",
    # Round 3 evidence. After round 2 removed "eight" from the tail entirely,
    # these are what took its place on held-out recorded speech — /h/-initial
    # or plosive-final ordinary words, again measured rather than guessed:
    #
    #   down    0.999997
    #   happy   0.999995
    #   stop    0.992592
    #   yes     0.987031
    #   left    0.966623
    #   no      0.935737
    "down.",
    "happy.",
    "stop.",
    "yes.",
    "left.",
    "no.",
)


#: espeak-ng voices used to phonemize the wake phrase, each giving a different
#: accent's realisation of it. The acoustic model is the same LibriTTS-R
#: generator throughout; what changes is the phoneme string it is asked to
#: speak, which is where accent lives for a phoneme-driven synthesizer.
#:
#: Every one of these was checked against the generator's own
#: ``phoneme_id_map``: all twelve produce phonemes the model knows, so none of
#: them silently degrades to dropped symbols. The realisations they give of
#: "hey youtab" are genuinely different words to a detector:
#:
#:   en-us            hˈeɪ jˈuːɾæb    flapped /t/, General American
#:   en-gb-x-rp       hˈeɪ jˈuːtæb    hard /t/, Received Pronunciation
#:   en-gb-scotland   hˈeː jˈʉːtab    fronted /u/, monophthong /e/
#:   en-029           hˈeɪ jˈuːtab    Caribbean
#:   en-gb-x-gbcwmd   ˈeː jˈəutab     West Midlands, and h-dropping
#:   en-au            hˈeɪ jˈuːɾɛəb   æ-tensing, shared with NZ/ZA/IN/NYC
#:
#: Weighted toward the two most common in this product's user base, but not so
#: heavily that the others are token.
ACCENTS: tuple[tuple[str, int], ...] = (
    ("en-us", 6),
    ("en-gb", 3),
    ("en-gb-x-rp", 2),
    ("en-au", 2),
    ("en-in", 2),
    ("en-gb-scotland", 1),
    ("en-029", 1),
    ("en-gb-x-gbclan", 1),
    ("en-gb-x-gbcwmd", 1),
    ("en-us-nyc", 1),
    ("en-nz", 1),
    ("en-za", 1),
)

#: Everyday speech that must never wake the agent. Distinct from
#: SOFT_NEGATIVES, which are long read sentences: these are the short
#: conversational fragments an always-on microphone hears all day, including
#: the ones that start with "hey" and the ones that mention tabs, because those
#: are the two things the wake phrase is made of.
COMMON_PHRASES: tuple[str, ...] = (
    "hey, how are you.",
    "hey, can you hear me.",
    "hey, come here a second.",
    "hey, what time is it.",
    "hey, look at this.",
    "hey, did you see that.",
    "hey, i'll call you back.",
    "hey, no problem.",
    "hey, hang on a moment.",
    "hey, sorry about that.",
    "hey guys.",
    "hey everyone.",
    "hey, that's great.",
    "hey, over here.",
    "open a new tab.",
    "switch to the other tab.",
    "close that tab please.",
    "which tab was it in.",
    "the tab is still loading.",
    "check the network tab.",
    "i left it open in a tab.",
    "you can see it in the tab.",
    "yes please.",
    "no thank you.",
    "one moment.",
    "hold on.",
    "never mind.",
    "that's all for now.",
    "okay then.",
    "all right.",
    "thanks a lot.",
    "see you later.",
    "good morning.",
    "good night.",
    "what do you think.",
    "let me check.",
    "i'm not sure.",
    "sounds good to me.",
    "can you repeat that.",
    "just a second.",
)


def accents() -> list[str]:
    """The accent voices expanded by weight, in a stable order."""
    out: list[str] = []
    for voice, weight in ACCENTS:
        out.extend([voice] * weight)
    return out


def positive_texts() -> list[str]:
    """The positive spellings expanded by weight, in a stable order."""
    out: list[str] = []
    for text, weight in POSITIVE_SPELLINGS:
        out.extend([text] * weight)
    return out
