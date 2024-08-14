"API that accepts an audio file and returns a transcription"
import os
import json
from openai import AzureOpenAI
from presidio_analyzer import  AnalyzerEngine, RecognizerRegistry, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer.entities import OperatorConfig
from presidio_anonymizer import AnonymizerEngine
from cog import BasePredictor, Input, Path
from dotenv import load_dotenv
load_dotenv()

# Presidio Registry
PII_REG = RecognizerRegistry()
# Load a smaller NLP engine for lambda
configuration =  {
    "nlp_engine_name": "spacy",
    "models": [
        {"lang_code": "en", "model_name": "en_core_web_md"}
    ]
}
provider = NlpEngineProvider(nlp_configuration=configuration)
small_engine = provider.create_engine()
# Load Wanis's recognizers json
with open('recognizers.json', 'r', encoding='utf-8') as f:
    recognizers = json.load(f)
# Load predefined recognizers
PII_REG.load_predefined_recognizers()
# and add our recognizers 
for recognizer_name, recognizer_data in recognizers.items():
    pattern_recognizer = PatternRecognizer(
        supported_entity=recognizer_name,
        patterns=[Pattern(**recognizer_data['pattern'])], # pattern dict is unpacked to Pattern constructor
        context=recognizer_data['context']
    )
    PII_REG.add_recognizer(pattern_recognizer)

class Predictor(BasePredictor):
    "Generate Transcriptions and Summaries using the COG framework"
    def llm_transcribe(self, audio,lang=None):
        "Use Whisper to Transcribe"

        whisper_client = AzureOpenAI(
            api_key=os.getenv('AZURE_OPENAIWE_KEY'),
            azure_endpoint=os.getenv('AZURE_OPENAIWE_ENDPOINT'),
            api_version='2023-09-01-preview'
        )

        transcription = whisper_client.audio.transcriptions.create(
            file=open(str(audio),"rb"),
            language=lang,
            model='eit-whisper'
        ).text

        return {"audio_transcript": transcription}
    
    def scrubber(self, transcript: str, lang: str = 'en'):
        """
        Remove Personal Information & Postcode from the transcript
        """
        engine = AnonymizerEngine()
        analyzer = AnalyzerEngine(nlp_engine=small_engine,registry=PII_REG)

        analyzer_results = analyzer.analyze(text=transcript,
                                            language=lang,
                                            context=[
                                                'full name',
                                                'name',
                                                'postcode',
                                                'birth',
                                                'account',
                                                'address',
                                                'actor',
                                                'Actor Name',
                                                'Actor Name:'
                                                'Actor'
                                            ],
                                            )

        result = engine.anonymize(
            text=transcript,
            analyzer_results=analyzer_results,
            operators={"DEFAULT": OperatorConfig("replace", {"new_value": "<REDACTED>"})}) # Replace all detected PII with <REDACTED>

        return result.text



    def predict(
        self,
        audio: Path = Input(description="Audio file", default=None),
        lang: str = Input(description="Optional. Language spoken in the audio. Ignore for language detection", default=None),
    ) -> dict:
        """Transcribes and optionally translates a single audio file"""

        trans = self.llm_transcribe(
            audio=str(audio),
            lang=str(lang)
        )
        
        clean = self.scrubber(trans)

        return {
            "PII": trans,
            "NOPII": clean
        }
    
    
    
