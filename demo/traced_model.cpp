#include <opencv2/core/core.hpp>
#include <opencv2/highgui/highgui.hpp>
#include <opencv2/opencv.hpp>
#include <iostream>
#include <torch/script.h>

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cout << argv[0] << " image.jpg" << std::endl;
    return 1;
  }
  auto img_ = cv::imread(argv[1], cv::IMREAD_COLOR);
  cv::Mat img(480, 640, CV_8UC3);
  cv::resize(img_, img, img.size(), 0, 0, cv::INTER_AREA);
  auto input_ = torch::tensor(at::ArrayRef<uint8_t>(img.data, img.rows * img.cols * 3)).view({img.rows, img.cols, 3});

  std::shared_ptr<torch::jit::script::Module> module =
    torch::jit::load("end_to_end_model.pt");
  
  std::vector<torch::jit::IValue> inputs;
  inputs.push_back(input_);
  auto res = module->forward(inputs).toTensor();

  cv::Mat cv_res(res.size(0), res.size(1), CV_8UC3, (void*) res.data<uint8_t>());
  cv::namedWindow("Detected", cv::WINDOW_AUTOSIZE);
  cv::imshow("Detected", cv_res);

  cv::waitKey(0);
  return 0;
}
