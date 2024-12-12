#ifndef _BASE_CHANGE_LISTENER_H_INCLUDED_
#define _BASE_CHANGE_LISTENER_H_INCLUDED_

#include <optional>

template <class Value> class ChangeListener {
  public:
    void notify(
            const std::optional<Value>& last_value,
            const Value& value);
};

#endif  // _BASE_CHANGE_LISTENER_H_INCLUDED_