// =============================================================================
// test/support/mini_json.h — parser JSON mínimo (DOM) SOMENTE para os testes
// de vetores golden. Motivo: o ArduinoJson 7.2 armazena decimais curtos como
// float de 32 bits (ex.: 0.3 -> 0.30000001), o que inviabiliza a comparação
// com tolerância 1e-9; aqui os números usam std::strtod (arredondamento
// correto para double, igual ao json do Python).
// =============================================================================
#pragma once

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace semtest {
namespace json {

struct Value {
  enum class Type { Null, Bool, Number, String, Array, Object };
  Type type = Type::Null;
  bool b = false;
  double num = 0.0;
  std::string str;
  std::vector<Value> arr;
  std::vector<std::pair<std::string, Value>> obj;

  bool is_null() const { return type == Type::Null; }
  bool is_bool() const { return type == Type::Bool; }
  bool is_number() const { return type == Type::Number; }
  bool is_string() const { return type == Type::String; }
  bool is_array() const { return type == Type::Array; }
  bool is_object() const { return type == Type::Object; }

  /// Membro de objeto (ou null estático se ausente / não for objeto).
  const Value& operator[](const char* key) const {
    if (type == Type::Object) {
      for (const auto& kv : obj)
        if (kv.first == key) return kv.second;
    }
    return null_value();
  }
  const Value& at(size_t i) const { return (type == Type::Array && i < arr.size()) ? arr[i] : null_value(); }
  size_t size() const { return type == Type::Array ? arr.size() : (type == Type::Object ? obj.size() : 0); }

  double as_num(double def = 0.0) const { return is_number() ? num : def; }
  bool as_bool(bool def = false) const { return is_bool() ? b : def; }
  const char* as_str(const char* def = "") const { return is_string() ? str.c_str() : def; }

  static const Value& null_value() {
    static const Value kNull{};
    return kNull;
  }
};

class Parser {
 public:
  explicit Parser(const std::string& text) : s_(text.c_str()), end_(text.c_str() + text.size()) {}

  bool parse(Value* out, std::string* err) {
    ws();
    if (!value(out)) {
      if (err) *err = "JSON invalido perto de: " + std::string(p(), static_cast<size_t>(std::min<long>(40, end_ - p())));
      return false;
    }
    ws();
    if (s_ != end_) {
      if (err) *err = "lixo apos o JSON";
      return false;
    }
    return true;
  }

 private:
  const char* p() const { return s_; }
  void ws() {
    while (s_ < end_ && (*s_ == ' ' || *s_ == '\n' || *s_ == '\r' || *s_ == '\t')) ++s_;
  }
  bool lit(const char* w) {
    const size_t n = std::strlen(w);
    if (static_cast<size_t>(end_ - s_) < n || std::strncmp(s_, w, n) != 0) return false;
    s_ += n;
    return true;
  }
  static void utf8(unsigned cp, std::string* o) {
    if (cp < 0x80) {
      o->push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      o->push_back(static_cast<char>(0xC0 | (cp >> 6)));
      o->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
      o->push_back(static_cast<char>(0xE0 | (cp >> 12)));
      o->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      o->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      o->push_back(static_cast<char>(0xF0 | (cp >> 18)));
      o->push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
      o->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      o->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
  }
  bool hex4(unsigned* cp) {
    if (end_ - s_ < 4) return false;
    unsigned v = 0;
    for (int i = 0; i < 4; ++i) {
      const char c = *s_++;
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
      else return false;
    }
    *cp = v;
    return true;
  }
  bool string(std::string* o) {
    if (s_ >= end_ || *s_ != '"') return false;
    ++s_;
    while (s_ < end_ && *s_ != '"') {
      char c = *s_++;
      if (c != '\\') {
        o->push_back(c);
        continue;
      }
      if (s_ >= end_) return false;
      c = *s_++;
      switch (c) {
        case '"': o->push_back('"'); break;
        case '\\': o->push_back('\\'); break;
        case '/': o->push_back('/'); break;
        case 'b': o->push_back('\b'); break;
        case 'f': o->push_back('\f'); break;
        case 'n': o->push_back('\n'); break;
        case 'r': o->push_back('\r'); break;
        case 't': o->push_back('\t'); break;
        case 'u': {
          unsigned cp = 0;
          if (!hex4(&cp)) return false;
          if (cp >= 0xD800 && cp < 0xDC00 && end_ - s_ >= 6 && s_[0] == '\\' && s_[1] == 'u') {
            s_ += 2;
            unsigned lo = 0;
            if (!hex4(&lo)) return false;
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          }
          utf8(cp, o);
          break;
        }
        default: return false;
      }
    }
    if (s_ >= end_) return false;
    ++s_;
    return true;
  }
  bool value(Value* v) {
    ws();
    if (s_ >= end_) return false;
    const char c = *s_;
    if (c == '{') {
      ++s_;
      v->type = Value::Type::Object;
      ws();
      if (s_ < end_ && *s_ == '}') return ++s_, true;
      for (;;) {
        ws();
        std::string key;
        if (!string(&key)) return false;
        ws();
        if (s_ >= end_ || *s_++ != ':') return false;
        Value child;
        if (!value(&child)) return false;
        v->obj.emplace_back(std::move(key), std::move(child));
        ws();
        if (s_ < end_ && *s_ == ',') { ++s_; continue; }
        if (s_ < end_ && *s_ == '}') return ++s_, true;
        return false;
      }
    }
    if (c == '[') {
      ++s_;
      v->type = Value::Type::Array;
      ws();
      if (s_ < end_ && *s_ == ']') return ++s_, true;
      for (;;) {
        Value child;
        if (!value(&child)) return false;
        v->arr.push_back(std::move(child));
        ws();
        if (s_ < end_ && *s_ == ',') { ++s_; continue; }
        if (s_ < end_ && *s_ == ']') return ++s_, true;
        return false;
      }
    }
    if (c == '"') {
      v->type = Value::Type::String;
      return string(&v->str);
    }
    if (lit("true")) { v->type = Value::Type::Bool; v->b = true; return true; }
    if (lit("false")) { v->type = Value::Type::Bool; v->b = false; return true; }
    if (lit("null")) { v->type = Value::Type::Null; return true; }
    if (lit("NaN") || lit("Infinity") || lit("-Infinity")) return false;  // não é JSON válido
    char* stop = nullptr;
    const double d = std::strtod(s_, &stop);
    if (stop == s_) return false;
    s_ = stop;
    v->type = Value::Type::Number;
    v->num = d;
    return true;
  }

  const char* s_;
  const char* end_;
};

}  // namespace json
}  // namespace semtest
