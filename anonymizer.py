import logging
import re
import time
from natasha import NamesExtractor, MorphVocab

# Configure logging to console
logger = logging.getLogger("DataAnonymizer")
logger.setLevel(logging.INFO)

# Avoid adding duplicate handlers if the logger is reloaded
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class DataAnonymizer:
    """
    DataAnonymizer performs high-performance PII (Personally Identifiable Information) masking.
    It censors:
    - Russian Names/Surnames/Patronymics (ФИО) -> [ФИО]
    - Phone numbers -> [ТЕЛЕФОН]
    - Email addresses -> [EMAIL]
    - Links (http/https/tg/t.me) -> [ССЫЛКА]
    
    Optimized for high-speed concurrent lookups by pre-compiling regexes and
    caching the heavy Natasha NamesExtractor once upon instantiation.
    """
    def __init__(self):
        logger.info("Initializing DataAnonymizer components...")
        start_init = time.perf_counter()

        # Initialize Natasha MorphVocab and NamesExtractor.
        # This is a one-time cost, done on application startup.
        self.morph_vocab = MorphVocab()
        self.names_extractor = NamesExtractor(self.morph_vocab)

        # Pre-compile regular expressions to achieve sub-millisecond substitution speed.
        # Handles various Russian phone formats (e.g. +7, 8, spaces, hyphens, parentheses).
        self.phone_regex = re.compile(
            r'(?:\+?7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}|\b(?:\+?7|8)\d{10}\b'
        )
        
        # standard, RFC-compliant email regex
        self.email_regex = re.compile(
            r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
        )
        
        # Link regex to catch http/https/tg:// schemes and t.me short links.
        # Excludes standard trailing punctuation like commas or periods from the link itself.
        self.link_regex = re.compile(
            r'\b(?:https?://|tg://)[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=]+|t\.me/[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=]+'
        )

        end_init = time.perf_counter()
        logger.info(f"DataAnonymizer components loaded successfully in {end_init - start_init:.4f} seconds.")

    def anonymize_text(self, text: str) -> str:
        """
        Anonymizes a text string by replacing PII with designated tokens.
        
        :param text: Input text from a student query.
        :return: Clean, anonymized text ready to be sent to external LLMs.
        """
        if not text:
            return ""

        start_total = time.perf_counter()
        logger.info(f"Processing anonymization request. Input text length: {len(text)} characters.")

        # --- Phase 1: Regex Masking (Phones, Emails, Links) ---
        start_regex = time.perf_counter()
        
        # Mask links first to prevent emails or user handles in URLs from being matched twice
        anonymized_text = self.link_regex.sub("[ССЫЛКА]", text)
        anonymized_text = self.phone_regex.sub("[ТЕЛЕФОН]", anonymized_text)
        anonymized_text = self.email_regex.sub("[EMAIL]", anonymized_text)
        
        end_regex = time.perf_counter()
        logger.info(f"Phase 1: Regex masking completed in {end_regex - start_regex:.6f} seconds.")

        # --- Phase 2: Natasha Named Entity Recognition for Names (ФИО) ---
        start_natasha = time.perf_counter()
        
        try:
            matches = self.names_extractor(anonymized_text)
            # Filter matches to ensure they start with an uppercase letter.
            # Russian proper names are capitalized, and this avoids false positives
            # on lowercase common words like prepositions, nouns, and adjectives.
            matches_list = [
                m for m in matches 
                if m.start < len(anonymized_text) and anonymized_text[m.start].isupper()
            ]
            
            if matches_list:
                logger.info(f"Phase 2: Natasha detected {len(matches_list)} name entit(ies) to mask.")
                
                # Sort matches by start index descending to replace spans from right to left.
                # This guarantees that index offsets do not break the indices of subsequent matches.
                matches_list.sort(key=lambda m: m.start, reverse=True)
                
                for match in matches_list:
                    anonymized_text = (
                        anonymized_text[:match.start] + 
                        "[ФИО]" + 
                        anonymized_text[match.stop:]
                    )
            else:
                logger.info("Phase 2: Natasha detected no name entities in the text.")
        except Exception as e:
            logger.error(f"Error during Natasha NER processing: {e}", exc_info=True)
            # Fail-safe: return the regex-anonymized text if Natasha crashes
            
        end_natasha = time.perf_counter()
        logger.info(f"Phase 2: Natasha NER masking completed in {end_natasha - start_natasha:.6f} seconds.")

        total_duration = time.perf_counter() - start_total
        logger.info(
            f"Anonymization finished in {total_duration:.6f} seconds. "
            f"Output text length: {len(anonymized_text)} characters."
        )

        return anonymized_text
