# llm_client.py
# This module handles all communication with Language Models (LLMs)
# Extract this file and place it in the same directory as codude.py

import logging
import requests
import json
from urllib.parse import urlparse, urljoin
from PyQt5.QtCore import QThread, pyqtSignal


class LLMRequestThread(QThread):
    """Thread for sending requests to LLM and receiving responses"""
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    
    def __init__(self, llm_config, prompt, text, timeout=60, require_usetools=False):
        QThread.__init__(self)
        self.llm_config = llm_config
        self.prompt = prompt
        self.text = text
        self.timeout = timeout
        self.require_usetools = require_usetools
    
    def run(self):
        raw_response = "N/A"
        try:
            provider = self.llm_config.get("provider", "Local OpenAI-Compatible")
            llm_url = self.llm_config.get("url", "")
            api_key = self.llm_config.get("api_key", "")
            model_name = self.llm_config.get("model_name", "gpt-3.5-turbo")
            mcp_plugin_ids = self.llm_config.get("mcp_plugin_ids", "")
            user_content = f"{self.prompt}\n\nText: {self.text}" if self.text.strip() else self.prompt
            
            # Check for and strip USETOOLS: keyword from the beginning of prompt
            prompt_has_usetools = False
            if user_content.startswith("USETOOLS:"):
                user_content = user_content[9:].lstrip()
                prompt_has_usetools = True
                logging.debug("USETOOLS: keyword detected and stripped from prompt")
            
            headers = {"Content-Type": "application/json"}
            request_url = ""
            
            if provider == "Local OpenAI-Compatible":
                if not llm_url:
                    self.error_occurred.emit("LLM URL for Local provider not configured.")
                    return
                parsed_url = urlparse(llm_url)
                path = parsed_url.path.rstrip('/')
                if not path or path == '/':
                    base_url = llm_url.rstrip('/')
                    request_url = urljoin(f"{base_url}/", 'v1/chat/completions')
                    logging.info(f"Appended '/v1/chat/completions'. Using: {request_url}")
                elif path.endswith('/v1/chat/completions'):
                    request_url = llm_url
                else:
                    request_url = llm_url
                    logging.warning(f"Using provided local URL as is: {request_url}. Ensure it's the correct chat completion endpoint.")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                    logging.debug("Using API token for Local OpenAI-Compatible provider")
                messages = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": user_content}
                ]
                payload = {"model": model_name, "messages": messages}
            
            elif provider == "OpenAI API":
                if not api_key:
                    self.error_occurred.emit("OpenAI API Key not configured.")
                    return
                request_url = "https://api.openai.com/v1/chat/completions"
                headers["Authorization"] = f"Bearer {api_key}"
                messages = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": user_content}
                ]
                payload = {"model": model_name, "messages": messages}
            
            elif provider == "LM Studio Native API":
                if not llm_url:
                    self.error_occurred.emit("LM Studio URL not configured.")
                    return
                parsed_url = urlparse(llm_url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                request_url = urljoin(f"{base_url}/", 'api/v1/chat')
                headers["Authorization"] = f"Bearer {api_key}" if api_key else ""
                payload = {"model": model_name, "input": user_content}
                
                logging.debug(f"MCP plugin IDs raw value: '{mcp_plugin_ids}'")
                logging.debug(f"require_usetools flag: {self.require_usetools}, prompt_has_usetools: {prompt_has_usetools}")
                
                should_use_tools = mcp_plugin_ids and mcp_plugin_ids.strip()
                if self.require_usetools:
                    should_use_tools = should_use_tools and prompt_has_usetools
                
                if should_use_tools:
                    plugin_list = [p.strip() for p in mcp_plugin_ids.split(',') if p.strip()]
                    logging.debug(f"Parsed plugin list: {plugin_list}")
                    if plugin_list:
                        payload["integrations"] = [{"type": "plugin", "id": plugin_id} for plugin_id in plugin_list]
                        logging.info(f"MCP integrations added for LM Studio Native API: {plugin_list}")
                    else:
                        logging.warning("No valid plugin IDs found after parsing")
                else:
                    if self.require_usetools and not prompt_has_usetools:
                        logging.debug("Tools disabled: require_usetools is True but prompt lacks USETOOLS: keyword")
                    else:
                        logging.debug("No MCP plugin IDs specified")
            else:
                self.error_occurred.emit(f"Unsupported LLM provider: {provider}")
                return

            logging.debug(f"Sending LLM request to {request_url} for provider {provider} with model {model_name}")
            response = requests.post(request_url, json=payload, headers=headers, timeout=self.timeout)
            raw_response = response.text
            
            if response.status_code != 200:
                logging.error(f"LLM request failed with status {response.status_code}. Response: {raw_response[:500]}...")
                error_msg = f"LLM request failed (Status: {response.status_code})."
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict) and 'error' in error_data:
                        if isinstance(error_data['error'], dict) and 'message' in error_data['error']:
                            error_msg += f" Message: {error_data['error']['message']}"
                        elif isinstance(error_data['error'], str):
                            error_msg += f" Message: {error_data['error']}"
                except json.JSONDecodeError:
                    error_msg += f" Raw Response: {raw_response[:200]}"
                except Exception as parse_err:
                    logging.error(f"Failed to parse LLM error response: {parse_err}")
                    error_msg += f" Raw Response: {raw_response[:200]}"
                self.error_occurred.emit(error_msg)
                return

            logging.debug(f"Raw LLM success response: {raw_response[:500]}...")
            result = response.json()
            if not result:
                raise ValueError("Empty success response from LLM")
            if not isinstance(result, dict):
                raise ValueError(f"Invalid success response format. Expected dict, got {type(result)}")
            
            # Handle different response formats
            content = None
            if provider == "LM Studio Native API":
                output = result.get('output')
                if isinstance(output, list) and output:
                    for item in reversed(output):
                        if isinstance(item, dict) and item.get('type') == 'message' and item.get('content'):
                            content = item.get('content')
                            break
                    if content is None:
                        first_output = output[0]
                        if isinstance(first_output, dict):
                            content = first_output.get('content')
                if content is None:
                    content = result.get('content') or result.get('text') or result.get('response')
            else:
                # OpenAI-compatible format
                choices = result.get('choices')
                if isinstance(choices, list) and choices:
                    first_choice = choices[0]
                    if isinstance(first_choice, dict):
                        message = first_choice.get('message')
                        if isinstance(message, dict):
                            content = message.get('content')
                if content is None:
                    if 'text' in result and isinstance(result['text'], str):
                        content = result['text']
                        logging.debug("Extracted content using fallback 'text' field.")
                    elif 'response' in result and isinstance(result['response'], str):
                        content = result['response']
                        logging.debug("Extracted content using fallback 'response' field.")
            
            if content is None:
                raise ValueError("No valid content found in LLM success response.")
            if not isinstance(content, str):
                raise ValueError(f"Invalid content type found: {type(content)}. Expected string.")
            self.response_received.emit(content)
        
        except requests.exceptions.Timeout:
            self.error_occurred.emit(f"LLM request timed out after {self.timeout} seconds.")
        except requests.exceptions.RequestException as e:
            self.error_occurred.emit(f"Error communicating with LLM: {e}")
        except json.JSONDecodeError as e:
            self.error_occurred.emit(f"Failed to decode LLM JSON response: {e}\nRaw response glimpse: {raw_response[:200]}")
        except ValueError as e:
            self.error_occurred.emit(f"Invalid LLM response: {e}")
        except Exception as e:
            logging.error(f"Unexpected error in LLMRequestThread: {e}", exc_info=True)
            self.error_occurred.emit(f"Unexpected error: {e}")