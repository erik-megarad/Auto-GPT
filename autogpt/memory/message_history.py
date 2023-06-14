from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autogpt.agent import Agent

from autogpt.config import Config
from autogpt.json_utils.utilities import (
    extract_json_from_response,
)
from autogpt.llm.base import (
    ChatSequence,
    Message,
    MessageCycle,
)
from autogpt.llm.providers.openai import OPEN_AI_CHAT_MODELS
from autogpt.llm.utils import count_string_tokens, create_chat_completion
from autogpt.log_cycle.log_cycle import PROMPT_SUMMARY_FILE_NAME, SUMMARY_FILE_NAME
from autogpt.logs import logger


@dataclass
class MessageHistory:
    agent: Agent

    message_cycles: list[Message] = field(default_factory=list)
    summary: str = "I was created"

    last_trimmed_index: int = 0

    def __getitem__(self, i: int):
        return self.message_cycles[i]

    def __iter__(self):
        return iter(self.message_cycles)

    def __len__(self):
        return len(self.message_cycles)

    def add(self, message_cycle: MessageCycle):
        return self.append(message_cycle)

    def append(self, message_cycle: MessageCycle):
        return self.message_cycles.append(message_cycle)

    @property
    def messages(self):
        messages = []
        for cycle in self.message_cycles:
            messages += cycle.messages

        return messages

    def trim_messages(
        self,
        current_message_chain: list[Message],
    ) -> tuple[Message, list[Message]]:
        """
        Returns a list of trimmed messages: messages which are in the message history
        but not in current_message_chain.

        Args:
            current_message_chain (list[Message]): The messages currently in the context.

        Returns:
            Message: A message with the new running summary after adding the trimmed messages.
            list[Message]: A list of messages that are in full_message_history with an index higher than last_trimmed_index and absent from current_message_chain.
        """
        # Select messages in full_message_history with an index higher than last_trimmed_index
        new_messages = [
            msg for i, msg in enumerate(self.messages) if i > self.last_trimmed_index
        ]

        # Remove messages that are already present in current_message_chain
        new_messages_not_in_chain = [
            msg for msg in new_messages if msg not in current_message_chain
        ]

        if not new_messages_not_in_chain:
            return self.summary_message(), []

        new_summary_message = self.update_running_summary(
            new_events=new_messages_not_in_chain
        )

        # Find the index of the last message processed
        last_message = new_messages_not_in_chain[-1]
        self.last_trimmed_index = self.message_cycles.index(last_message)

        return new_summary_message, new_messages_not_in_chain

    def per_cycle(self):
        """
        Yields:
            MessageCycle
        """
        for cycle in self.message_cycles:
            yield cycle

    def summary_message(self) -> Message:
        return Message(
            "system",
            f"This reminds you of these events from your past: \n{self.summary}",
        )

    def update_running_summary(self, new_events: list[Message]) -> Message:
        """
        This function takes a list of dictionaries representing new events and combines them with the current summary,
        focusing on key and potentially important information to remember. The updated summary is returned in a message
        formatted in the 1st person past tense.

        Args:
            new_events (List[Dict]): A list of dictionaries containing the latest events to be added to the summary.

        Returns:
            str: A message containing the updated summary of actions, formatted in the 1st person past tense.

        Example:
            new_events = [{"event": "entered the kitchen."}, {"event": "found a scrawled note with the number 7"}]
            update_running_summary(new_events)
            # Returns: "This reminds you of these events from your past: \nI entered the kitchen and found a scrawled note saying 7."
        """
        cfg = Config()

        if not new_events:
            return self.summary_message()

        # Create a copy of the new_events list to prevent modifying the original list
        new_events = copy.deepcopy(new_events)

        # Replace "assistant" with "you". This produces much better first person past tense results.
        for event in new_events:
            if event.role.lower() == "assistant":
                event.role = "you"

                # Remove "thoughts" dictionary from "content"
                try:
                    content_dict = extract_json_from_response(event.content)
                    if "thoughts" in content_dict:
                        del content_dict["thoughts"]
                    event.content = json.dumps(content_dict)
                except json.JSONDecodeError as e:
                    logger.error(f"Error: Invalid JSON: {e}")
                    if cfg.debug_mode:
                        logger.error(f"{event.content}")

            elif event.role.lower() == "system":
                event.role = "your computer"

            # Delete all user messages
            elif event.role == "user":
                new_events.remove(event)

        # Summarize events and current summary in batch to a new running summary

        # Assume an upper bound length for the summary prompt template, i.e. Your task is to create a concise running summary...., in summarize_batch func
        # TODO make this default dynamic
        prompt_template_length = 100
        max_tokens = OPEN_AI_CHAT_MODELS.get(cfg.fast_llm_model).max_tokens
        summary_tlength = count_string_tokens(str(self.summary), cfg.fast_llm_model)
        batch = []
        batch_tlength = 0

        # TODO Can put a cap on length of total new events and drop some previous events to save API cost, but need to think thru more how to do it without losing the context
        for event in new_events:
            event_tlength = count_string_tokens(str(event), cfg.fast_llm_model)

            if (
                batch_tlength + event_tlength
                > max_tokens - prompt_template_length - summary_tlength
            ):
                # The batch is full. Summarize it and start a new one.
                self.summarize_batch(batch, cfg)
                summary_tlength = count_string_tokens(
                    str(self.summary), cfg.fast_llm_model
                )
                batch = [event]
                batch_tlength = event_tlength
            else:
                batch.append(event)
                batch_tlength += event_tlength

        if batch:
            # There's an unprocessed batch. Summarize it.
            self.summarize_batch(batch, cfg)

        return self.summary_message()

    def summarize_batch(self, new_events_batch, cfg):
        prompt = f'''Your task is to create a concise running summary of actions and information results in the provided text, focusing on key and potentially important information to remember.

You will receive the current summary and your latest actions. Combine them, adding relevant key information from the latest development in 1st person past tense and keeping the summary concise.

Summary So Far:
"""
{self.summary}
"""

Latest Development:
"""
{new_events_batch or "Nothing new happened."}
"""
'''

        prompt = ChatSequence.for_model(cfg.fast_llm_model, [Message("user", prompt)])
        self.agent.log_cycle_handler.log_cycle(
            self.agent.ai_name,
            self.agent.created_at,
            self.agent.cycle_count,
            prompt.raw(),
            PROMPT_SUMMARY_FILE_NAME,
        )

        self.summary = create_chat_completion(prompt).content

        self.agent.log_cycle_handler.log_cycle(
            self.agent.ai_name,
            self.agent.created_at,
            self.agent.cycle_count,
            self.summary,
            SUMMARY_FILE_NAME,
        )
