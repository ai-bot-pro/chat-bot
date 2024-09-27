from threading import Thread

from .base import TransformersBaseLLM
from src.common.session import Session
from src.types.speech.language import TO_LLM_LANGUAGE


class TransformersManualLLM(TransformersBaseLLM):
    TAG = "llm_transformers_manual"

    def warmup(self):
        dummy_input_text = self.args.warnup_prompt
        dummy_msgs = [{"role": self.args.user_role, "content": dummy_input_text}]
        text = self._tokenizer.apply_chat_template(
            dummy_msgs,
            tokenize=False,
            add_generation_prompt=True,
        )
        model_inputs = self._tokenizer(
            [text], return_tensors="pt").to(self._model.device)

        warmup_gen_kwargs = dict(
            model_inputs,
            streamer=self._streamer,
            min_new_tokens=self.args.lm_gen_min_new_tokens,
            max_new_tokens=self.args.lm_gen_max_new_tokens,
            do_sample=self.args.lm_gen_do_sample,
            temperature=self.args.lm_gen_temperature,
            repetition_penalty=self.args.lm_gen_repetition_penalty,
        )

        self._warmup(target=self._model.generate, kwargs=warmup_gen_kwargs)

    def generate(self, session: Session):
        prompt = session.ctx.state['prompt']
        if isinstance(prompt, tuple):
            prompt, language_code = prompt
            if isinstance(prompt, str):
                prompt = f"Please reply to my message in {TO_LLM_LANGUAGE[language_code]}. " + prompt

        self._chat_history.append({'role': self.args.user_role, 'content': prompt})
        text = self._tokenizer.apply_chat_template(
            self._chat_history.to_list(),
            tokenize=False,
            add_generation_prompt=True,
        )
        model_inputs = self._tokenizer([text], return_tensors="pt").to(self._model.device)

        generation_kwargs = dict(
            model_inputs,
            streamer=self._streamer,
            do_sample=self.args.lm_gen_do_sample,
            temperature=self.args.lm_gen_temperature,
            repetition_penalty=self.args.lm_gen_repetition_penalty,
            min_new_tokens=self.args.lm_gen_min_new_tokens,
            max_new_tokens=self.args.lm_gen_max_new_tokens)
        thread = Thread(target=self._model.generate, kwargs=generation_kwargs)
        thread.start()

        generated_text = ""
        for new_text in self._streamer:
            generated_text += new_text
            yield new_text
        self._chat_history.append({"role": "assistant", "content": generated_text})
