#ifndef _BASE_CHANGE_NOTIFIER_H_INCLUDED_
#define _BASE_CHANGE_NOTIFIER_H_INCLUDED_

#include <algorithm>
#include <memory>
#include <optional>

#include "base/change-listener.h"

template <class Value> class ChangeNotifier {
  public:
    void registerListener(const std::shared_ptr<ChangeListener<Value>>& listener) {
        listeners_.push_back(listener);
    }
    void setValue(const Value& value) {
        if (!lastValue_.has_value() || lastValue_.value() != value) {
            int empty_listeners = 0;
            for (std::weak_ptr<ChangeListener<Value>> listener : listeners_) {
                std::shared_ptr<ChangeListener<Value>> shared_listener = listener.lock();
                if (shared_listener) {
                    shared_listener->notify(lastValue_, value);
                } else {
                    empty_listeners++;
                }
            }
            if (empty_listeners > 10
                    && empty_listeners > listeners_.size() / 2) {
                listeners_.erase(
                    std::remove_if(
                        listeners_.begin(),
                        listeners_.end(),
                        [](const std::weak_ptr<ChangeListener<Value>>& x) {
                            return x.expired();
                        }),
                    listeners_.end());
            }
        }
    }

    std::optional<Value> lastValue() const { return lastValue_; }
  private:
    std::optional<Value> lastValue_;
    std::vector<std::weak_ptr<ChangeListener<Value>>> listeners_;
};

#endif  // _BASE_CHANGE_NOTIFIER_H_INCLUDED_