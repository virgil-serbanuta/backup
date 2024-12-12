#ifndef _BASE_THING_WITH_ERROR_H_INCLUDED_
#define _BASE_THING_WITH_ERROR_H_INCLUDED_

#include <memory>
#include <utility>

template<class Thing, class Error> class ThingWithError {
  public:
    ThingWithError(std::unique_ptr<Thing> thing)
        : is_error_(false), thing_(std::move(thing)) {}
    ThingWithError(std::unique_ptr<Error> error)
        : is_error_(true), error_(std::move(error)) {}

    bool isError() const { return is_error_; }
    bool isValue() const { return !is_error_; }
    const Error& error() const { return *error_; }
    const Thing& value() const { return *thing_; }
  private:
    bool is_error_;
    std::unique_ptr<Thing> thing_;
    std::unique_ptr<Error> error_;
};

#endif  // _BASE_THING_WITH_ERROR_H_INCLUDED_