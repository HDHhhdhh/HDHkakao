using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Spoiler
{
    /// <summary>
    /// Tiny JSON reader used for untrusted bridge lines.  It supports the JSON
    /// types needed by the NDJSON contract and has no code path that executes
    /// values as commands.
    /// </summary>
    internal static class LocoJson
    {
        public static object Parse(string json)
        {
            JsonReader reader = new JsonReader(json);
            object value = reader.ReadValue();
            reader.RequireEnd();
            return value;
        }

        private sealed class JsonReader
        {
            private readonly string _json;
            private int _position;

            public JsonReader(string json)
            {
                _json = json ?? string.Empty;
            }

            public object ReadValue()
            {
                SkipWhitespace();
                if (_position >= _json.Length) throw new FormatException("JSON value is missing.");
                char character = _json[_position];
                if (character == '{') return ReadObject();
                if (character == '[') return ReadArray();
                if (character == '"') return ReadString();
                if (character == 't' && Consume("true")) return true;
                if (character == 'f' && Consume("false")) return false;
                if (character == 'n' && Consume("null")) return null;
                return ReadNumber();
            }

            public void RequireEnd()
            {
                SkipWhitespace();
                if (_position != _json.Length) throw new FormatException("Trailing JSON content.");
            }

            private Dictionary<string, object> ReadObject()
            {
                Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                _position++;
                SkipWhitespace();
                if (Consume("}")) return result;

                while (true)
                {
                    SkipWhitespace();
                    if (_position >= _json.Length || _json[_position] != '"') throw new FormatException("JSON object key is missing.");
                    string key = ReadString();
                    SkipWhitespace();
                    if (!Consume(":")) throw new FormatException("JSON object colon is missing.");
                    result[key] = ReadValue();
                    SkipWhitespace();
                    if (Consume("}")) return result;
                    if (!Consume(",")) throw new FormatException("JSON object separator is missing.");
                }
            }

            private List<object> ReadArray()
            {
                List<object> result = new List<object>();
                _position++;
                SkipWhitespace();
                if (Consume("]")) return result;
                while (true)
                {
                    result.Add(ReadValue());
                    SkipWhitespace();
                    if (Consume("]")) return result;
                    if (!Consume(",")) throw new FormatException("JSON array separator is missing.");
                }
            }

            private string ReadString()
            {
                if (_position >= _json.Length || _json[_position] != '"') throw new FormatException("JSON string is missing.");
                _position++;
                StringBuilder result = new StringBuilder();
                while (_position < _json.Length)
                {
                    char character = _json[_position++];
                    if (character == '"') return result.ToString();
                    if (character != '\\')
                    {
                        if (character < 0x20) throw new FormatException("JSON string contains a control character.");
                        result.Append(character);
                        continue;
                    }

                    if (_position >= _json.Length) throw new FormatException("JSON escape is incomplete.");
                    char escaped = _json[_position++];
                    switch (escaped)
                    {
                        case '"': result.Append('"'); break;
                        case '\\': result.Append('\\'); break;
                        case '/': result.Append('/'); break;
                        case 'b': result.Append('\b'); break;
                        case 'f': result.Append('\f'); break;
                        case 'n': result.Append('\n'); break;
                        case 'r': result.Append('\r'); break;
                        case 't': result.Append('\t'); break;
                        case 'u':
                            if (_position + 4 > _json.Length) throw new FormatException("JSON unicode escape is incomplete.");
                            int code = int.Parse(_json.Substring(_position, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                            result.Append((char)code);
                            _position += 4;
                            break;
                        default: throw new FormatException("JSON escape is invalid.");
                    }
                }
                throw new FormatException("JSON string is unterminated.");
            }

            private object ReadNumber()
            {
                int start = _position;
                if (_position < _json.Length && _json[_position] == '-') _position++;
                while (_position < _json.Length && char.IsDigit(_json[_position])) _position++;
                if (_position < _json.Length && _json[_position] == '.')
                {
                    _position++;
                    while (_position < _json.Length && char.IsDigit(_json[_position])) _position++;
                }
                if (_position < _json.Length && (_json[_position] == 'e' || _json[_position] == 'E'))
                {
                    _position++;
                    if (_position < _json.Length && (_json[_position] == '+' || _json[_position] == '-')) _position++;
                    while (_position < _json.Length && char.IsDigit(_json[_position])) _position++;
                }

                string value = _json.Substring(start, _position - start);
                long integer;
                if (long.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out integer)) return integer;
                double number;
                if (double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out number)) return number;
                throw new FormatException("JSON number is invalid.");
            }

            private bool Consume(string value)
            {
                if (_position + value.Length > _json.Length || !string.Equals(_json.Substring(_position, value.Length), value, StringComparison.Ordinal)) return false;
                _position += value.Length;
                return true;
            }

            private void SkipWhitespace()
            {
                while (_position < _json.Length && char.IsWhiteSpace(_json[_position])) _position++;
            }
        }
    }
}
