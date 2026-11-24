"""Cross-request batches for strands-decider 0.4.0 model checkpoints (Strands Decider v2).

Upstream's DecisionEngine answers one request per forward. This adapter renders each request
with upstream's core, runs every (request, question) prompt of a microbatch as one full pass
under a per-row prefix_bidir mask, and reads the answers with upstream's readout. A full pass
per question is upstream's reference (`reuse_state=False`); only the batch shape differs.

Requests with images, or whose prompts exceed the batch token budget, use the upstream engine
unchanged. Only one executor thread may call `evaluate`.
"""


def prefix_bidir_mask(state_lengths, lengths, width, device=None):
    """[rows, 1, width, width] booleans: a state token sees its own state; a question token sees
    the state and its own question; nobody sees padding. Padding queries see every valid key, so
    no row of the mask is empty (SDPA would return NaN for it); their outputs are discarded."""
    import torch

    s = torch.tensor(state_lengths, device=device).view(-1, 1, 1)
    n = torch.tensor(lengths, device=device).view(-1, 1, 1)
    positions = torch.arange(width, device=device)
    queries, keys = positions.view(1, -1, 1), positions.view(1, 1, -1)
    return ((keys < n) & ((queries >= s) | (keys < s)))[:, None]


class BatchEngine:
    def __init__(self, engine, max_rows=256, max_tokens=8192, stable_choice_margin=0.05):
        self.engine = engine
        self.max_rows = max_rows
        self.max_tokens = max_tokens
        self.stable_choice_margin = stable_choice_margin

    def _forward(self, rows):
        """Hidden states [rows, hidden] at each prompt's last token, float32 numpy."""
        import torch

        backend = self.engine.backend
        width = max(len(row['ids']) for row in rows)
        if width % 64 == 1:  # upstream avoids 64k+1-wide attention (a torch CUDA SDPA bug)
            width += 1
        ids = torch.full((len(rows), width), backend._pad_id, dtype=torch.long)
        for r, row in enumerate(rows):
            ids[r, :len(row['ids'])] = torch.tensor(row['ids'])
        with torch.inference_mode():
            mask = prefix_bidir_mask([row['state'] for row in rows], [len(row['ids']) for row in rows],
                                     width, backend.device)
            positions = torch.arange(width, device=backend.device).expand(len(rows), -1)
            hidden = backend._decode(ids.to(backend.device), mask, positions)
            last = torch.tensor([len(row['ids']) - 1 for row in rows], device=backend.device)
            out = hidden[torch.arange(len(rows), device=backend.device), last]
            # One device-to-host transfer per batch, not per question.
            return out.float().cpu().numpy()

    def _reference(self, row):
        """The upstream reference pass for one prompt, in a fixed singleton shape."""
        import torch

        backend = self.engine.backend
        with torch.inference_mode():
            state, question = row['ids'][:row['state']], row['ids'][row['state']:]
            return backend._full_pass(state, question, {}).float().cpu().numpy()

    def evaluate(self, requests):
        import numpy as np
        import torch
        from strands_decider.core import question_probabilities, read, render
        from strands_decider.refusals import RequestRefused
        from strands_decider.schema import SystemOneResponse

        eng = self.engine
        results = [None] * len(requests)
        rendered, prepared, direct = {}, [], []
        for index, request in enumerate(requests):
            if request.images:
                direct.append(index)
                continue
            try:
                rendered[index] = render(request, eng.tokenizer, max_length=eng.max_length)
            except ValueError as exc:
                results[index] = RequestRefused(str(exc))
                continue
            rows = [{'request': index, 'question': name,
                     'ids': list(q.state_ids + q.question_ids), 'state': len(q.state_ids)}
                    for name, q in rendered[index].questions.items()]
            if max(len(row['ids']) for row in rows) > self.max_tokens:
                # Long prompts: upstream encodes the state once and batches its questions.
                del rendered[index]
                direct.append(index)
            else:
                prepared.extend(rows)

        hidden = {}

        def score(rows):
            retry_smaller = False
            try:
                out = self._forward(rows)
            except torch.cuda.OutOfMemoryError:
                if len(rows) == 1:
                    raise
                retry_smaller = True
            if retry_smaller:
                # Leave the except block first: its traceback holds the failed forward's tensors.
                torch.cuda.empty_cache()
                middle = len(rows) // 2
                score(rows[:middle])
                score(rows[middle:])
                return
            for row, vector in zip(rows, out, strict=True):
                hidden[row['request'], row['question']] = (row, vector, len(rows))

        # Group similar lengths so a long prompt does not pad every short one in its forward.
        prepared.sort(key=lambda row: len(row['ids']))
        chunk, width = [], 0
        for row in prepared:
            next_width = max(width, len(row['ids']))
            if chunk and (len(chunk) >= self.max_rows or next_width * (len(chunk) + 1) > self.max_tokens):
                score(chunk)
                chunk, width = [], 0
            chunk.append(row)
            width = max(width, len(row['ids']))
        if chunk:
            score(chunk)

        for index, request_rendered in rendered.items():
            names = list(request_rendered.questions)
            entries = [hidden[index, name] for name in names]
            vectors = np.stack([vector for _, vector, _ in entries])
            probs = question_probabilities(vectors, eng.config, request_rendered, weight=eng.weight)
            for position, (name, (row, _, batch_rows)) in enumerate(zip(names, entries, strict=True)):
                # bf16 reductions vary slightly with batch shape. Settle close choices in the
                # reference shape so neighbouring traffic cannot decide which option wins.
                ranked = sorted(probs[name].tolist(), reverse=True)
                if (batch_rows > 1 and request_rendered.questions[name].kind == 'choice'
                        and len(ranked) > 1 and ranked[0] - ranked[1] < self.stable_choice_margin):
                    vectors[position] = self._reference(row)
            answers = read(vectors, eng.config, request_rendered, weight=eng.weight)
            results[index] = SystemOneResponse.model_validate({
                'model': eng.model_name, 'answers': answers,
                'usage': {'input_tokens': request_rendered.input_tokens, 'output_tokens': len(answers)},
            }).model_dump()

        for index in direct:
            try:
                results[index] = eng.evaluate(requests[index]).model_dump()
            except ValueError as exc:  # RequestRefused is a ValueError
                results[index] = exc
        return results
